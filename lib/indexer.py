#!/usr/bin/env python3
"""星忆索引器：L1 条目解析、FTS 预分词、增量入库。

S1 范围：只处理 <MEMD_MEMORY_DIR>/<bank>/*.md 的 H2 条目。
L0 镜像、turn 抽取在 S2/S3 接入，复用同一套 segment/facts 写入。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import paths  # noqa: E402  路径单一入口：改环境变量即可迁移，不要再新增硬编码

ROOT = paths.ROOT
BANKS_DIR = paths.BANKS_DIR
MEMORY_DIR = paths.MEMORY_DIR
TZ = timezone(timedelta(hours=8))
BANKS = paths.BANKS

try:  # 有 jieba 用它，没有退 bigram，保证零依赖可跑
    import jieba  # type: ignore

    _HAS_JIEBA = True
except Exception:  # pragma: no cover
    _HAS_JIEBA = False

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z0-9_.+#/-]{2,}")


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def segment(text: str) -> str:
    """把中英混排切成 FTS 能命中的 token 串。"""
    if _HAS_JIEBA:
        return " ".join(t for t in jieba.cut(text) if t.strip())
    out: list[str] = []
    for m in _CJK.finditer(text):
        run = m.group(0)
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    out.extend(_WORD.findall(text))
    return " ".join(out)


def connect(bank: str) -> sqlite3.Connection:
    conn = sqlite3.connect(BANKS_DIR / f"{bank}.db", timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def split_sections(path: Path) -> list[tuple[str, str]]:
    """按 H2 切条目，返回 [(标题, 正文)]。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"^## ", text, flags=re.M)
    out = []
    for part in parts[1:]:
        lines = part.split("\n", 1)
        title = lines[0].strip()
        body = lines[1] if len(lines) > 1 else ""
        out.append((title, body.strip()))
    return out


def index_file(conn: sqlite3.Connection, bank: str, path: Path, run_id: str) -> int:
    domain = path.stem
    content = path.read_text(encoding="utf-8", errors="replace")
    file_hash = sha(content)
    doc_id = sha(f"{bank}|{path}")[:32]

    conn.execute(
        """INSERT INTO documents(id,kind,profile,domain,path,content_hash,mtime,created_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(profile,path) DO UPDATE SET
             content_hash=excluded.content_hash, mtime=excluded.mtime, domain=excluded.domain""",
        (doc_id, "l1", bank, domain, str(path), file_hash, path.stat().st_mtime, now()),
    )
    # 修复 B1：软删（forget）/标错状态必须跨重索引存活；archived 允许复活
    prev = {
        r["canonical_key"]: r
        for r in conn.execute(
            "SELECT canonical_key,learned_at,status,recallable,deleted_at FROM facts "
            "WHERE doc_id=? AND origin='l1'",
            (doc_id,),
        )
    }
    # 修复 B3：单文件重索引也要清自己的 FTS 倒排，否则同一条目重复计分
    conn.execute(
        "DELETE FROM facts_fts WHERE fact_id IN "
        "(SELECT id FROM facts WHERE doc_id=? AND origin='l1')",
        (doc_id,),
    )
    conn.execute("DELETE FROM facts WHERE doc_id=? AND origin='l1'", (doc_id,))
    conn.execute("DELETE FROM doc_entries WHERE doc_id=?", (doc_id,))

    n = 0
    for title, body in split_sections(path):
        section = f"## {title}\n{body}"
        entry_hash = sha(section)
        fact_id = sha(f"{bank}|{path}|{title}")[:32]
        canonical = sha(f"{bank}|{domain}|{title}")[:16]
        # 正文只存文件（收敛）：库里只留索引；seg_text 仅用于 FTS 倒排
        text = None
        seg_text = f"{title}\n{body}" if body else title
        # 修复 B1：已被 forget（deleted）/标错（wrong）的条目，重索引不得复活
        p = prev.get(canonical)
        kept_status = p["status"] if p and p["status"] in ("deleted", "wrong") else "active"
        kept_recallable = 0 if kept_status != "active" else 1
        kept_deleted_at = p["deleted_at"] if (p and kept_status == "deleted") else None
        conn.execute(
            """INSERT INTO facts(id,doc_id,profile,domain,scope,canonical_key,value,
                 visibility,target,source_path,subject,predicate,topic,text,origin,
                 learned_at,status,recallable,no_consolidate,raw_hash,hash,deleted_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(profile,canonical_key) DO UPDATE SET
                 text=excluded.text, raw_hash=excluded.raw_hash, hash=excluded.hash,
                 doc_id=excluded.doc_id, source_path=excluded.source_path,
                 status=CASE WHEN facts.status IN ('deleted','wrong') THEN facts.status ELSE excluded.status END,
                 recallable=CASE WHEN facts.status IN ('deleted','wrong') THEN 0 ELSE excluded.recallable END,
                 deleted_at=CASE WHEN facts.status='deleted' THEN facts.deleted_at ELSE excluded.deleted_at END""",
            (
                fact_id, doc_id, bank, domain, f"{bank}/{domain}", canonical, title,
                "private", "memory", str(path), title, None, domain, text, "l1",
                (p["learned_at"] if p else now()), kept_status, kept_recallable, 0,
                entry_hash, entry_hash, kept_deleted_at,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO doc_entries(doc_id,entry_key,content_hash,first_seen_run,last_seen_run) "
            "VALUES(?,?,?,?,?)",
            (doc_id, title, entry_hash, run_id, run_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO fact_sources(fact_id,source_kind,doc_id,quote,quote_hash,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (fact_id, "l1", doc_id, "", sha(body[:500]), now()),
        )
        conn.execute(
            "INSERT INTO facts_fts(fact_id,text_seg) VALUES(?,?)", (fact_id, segment(seg_text))
        )
        n += 1
    return n


def index_bank(bank: str, run_id: str | None = None) -> dict:
    run_id = run_id or f"run-{datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}"
    d = MEMORY_DIR / bank
    files = sorted(d.glob("*.md")) if d.is_dir() else []
    conn = connect(bank)
    total = 0
    try:
        known = {
            r["path"]: r["id"]
            for r in conn.execute(
                "SELECT path,id FROM documents WHERE profile=? AND kind='l1'", (bank,)
            )
        }
        if not files and known:
            # 修复 B4：目录不存在 = 挂载异常 → 保护性跳过；目录存在但文件全删 → 归档留证
            if not d.is_dir():
                return {"bank": bank, "files": 0, "skipped": "no_files_but_known", "known": len(known)}
            for doc_id in known.values():
                conn.execute(
                    "UPDATE facts SET status='archived', recallable=0, archived_at=? "
                    "WHERE doc_id=? AND origin='l1'",
                    (now(), doc_id),
                )
                conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            conn.execute(
                "DELETE FROM facts_fts WHERE fact_id IN "
                "(SELECT id FROM facts WHERE origin='l1' AND status='archived')"
            )
            conn.commit()
            facts = conn.execute("SELECT count(*) FROM facts").fetchone()[0]
            return {"bank": bank, "files": 0, "entries": 0, "facts": facts,
                    "documents": 0, "archived_docs": len(known)}
        cur = {str(f) for f in files}
        archived = 0
        for path, doc_id in known.items():
            if path not in cur:
                conn.execute(
                    "UPDATE facts SET status='archived', recallable=0, archived_at=? "
                    "WHERE doc_id=? AND origin='l1'",
                    (now(), doc_id),
                )
                conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
                archived += 1
        conn.execute("DELETE FROM facts_fts WHERE fact_id IN (SELECT id FROM facts WHERE origin='l1')")
        for f in files:
            total += index_file(conn, bank, f, run_id)
        conn.commit()
        facts = conn.execute("SELECT count(*) FROM facts").fetchone()[0]
        docs = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
        return {"bank": bank, "files": len(files), "entries": total, "facts": facts,
                "documents": docs, "archived_docs": archived}
    finally:
        conn.close()


def index_all() -> list[dict]:
    return [index_bank(b) for b in BANKS if (MEMORY_DIR / b).is_dir()]


# ---------------- S2：L0 镜像 ----------------

L0_HOMES = paths.l0_homes()


def l0_paths(bank: str) -> list[tuple[Path, str]]:
    home = L0_HOMES.get(bank)
    if home is None:
        return []
    out: list[tuple[Path, str]] = []
    for name, target in (("MEMORY.md", "memory"), ("USER.md", "user")):
        p = home / "memories" / name
        if p.exists():
            out.append((p, target))
    return out


def _l0_lines(path: Path) -> list[str]:
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line in ("§", "---") or line.startswith("```"):
            continue
        out.append(line)
    return out


def index_l0_file(conn: sqlite3.Connection, bank: str, path: Path, target: str, run_id: str) -> int:
    content = path.read_text(encoding="utf-8", errors="replace")
    doc_id = sha(f"{bank}|{path}|l0")[:32]
    conn.execute(
        """INSERT INTO documents(id,kind,profile,domain,path,content_hash,mtime,created_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(profile,path) DO UPDATE SET
             content_hash=excluded.content_hash, mtime=excluded.mtime""",
        (doc_id, "l0", bank, target, str(path), sha(content), path.stat().st_mtime, now()),
    )
    lines = _l0_lines(path)
    present: set[str] = set()
    for line in lines:
        h = sha(line)
        present.add(h)
        canonical = sha(f"{bank}|{target}|{line}")[:16]
        fact_id = sha(f"{bank}|{target}|{line}")[:32]
        visibility = "blocked" if target == "user" else "private"
        conn.execute(
            """INSERT INTO facts(id,doc_id,profile,domain,scope,canonical_key,value,
                 visibility,target,source_path,subject,predicate,topic,text,origin,
                 learned_at,status,recallable,no_consolidate,raw_hash,hash)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(profile,canonical_key) DO UPDATE SET
                 text=excluded.text, raw_hash=excluded.raw_hash, hash=excluded.hash,
                 doc_id=excluded.doc_id, source_path=excluded.source_path,
                 status=CASE WHEN facts.status='deleted' THEN 'deleted' ELSE 'active' END,
                 archived_at=CASE WHEN facts.status='deleted' THEN facts.archived_at ELSE NULL END""",
            (
                fact_id, doc_id, bank, target, f"{bank}/{target}", canonical, "",
                visibility, target, str(path), None, None, target, None, "l0",
                now(), "active", 0, 1, h, h,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO doc_entries(doc_id,entry_key,content_hash,first_seen_run,last_seen_run) "
            "VALUES(?,?,?,?,?)",
            (doc_id, h[:16], h, run_id, run_id),
        )
    # 消失的行：仅 watcher 看到 → archived，保留证据
    for row in conn.execute(
        "SELECT id,raw_hash FROM facts WHERE doc_id=? AND origin='l0' AND status='active'", (doc_id,)
    ).fetchall():
        if row["raw_hash"] not in present:
            conn.execute(
                "UPDATE facts SET status='archived', recallable=0, archived_at=? WHERE id=?",
                (now(), row["id"]),
            )
    return len(lines)


PRESSURE_DIR = paths.PRESSURE_DIR
LIMITS = {"default": 8000, "maojiu": 6000, "lingyuan": 6000}


def write_pressure(bank: str) -> dict:
    home = L0_HOMES.get(bank)
    if home is None:
        return {}
    mem = home / "memories" / "MEMORY.md"
    if not mem.exists():
        return {}
    chars = len(mem.read_text(encoding="utf-8", errors="replace"))
    limit = LIMITS.get(bank, 8000)
    cfg = home / "config.yaml"
    if cfg.exists():
        m = re.search(r"memory_char_limit:\s*(\d+)", cfg.read_text(encoding="utf-8", errors="replace"))
        if m:
            limit = int(m.group(1))
    pct = round(chars / limit * 100, 1)
    state = "over" if pct >= 95 else ("pressure" if pct >= 75 else "ok")
    payload = {"profile": bank, "pct": pct, "limit": limit, "chars": chars, "ts": now(), "state": state}
    PRESSURE_DIR.mkdir(parents=True, exist_ok=True)
    (PRESSURE_DIR / f"{bank}.json").write_text(
        __import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return payload


def index_bank_l0(bank: str) -> dict:
    conn = connect(bank)
    run_id = f"l0-{datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}"
    total = 0
    try:
        for path, target in l0_paths(bank):
            total += index_l0_file(conn, bank, path, target, run_id)
        conn.commit()
        active = conn.execute(
            "SELECT count(*) FROM facts WHERE profile=? AND origin='l0' AND status='active'", (bank,)
        ).fetchone()[0]
        pressure = write_pressure(bank)
        return {"bank": bank, "lines": total, "active": active, "pressure": pressure}
    finally:
        conn.close()


def mirror_diff(bank: str) -> dict:
    conn = connect(bank)
    try:
        src: dict[str, str] = {}
        for path, target in l0_paths(bank):
            for line in _l0_lines(path):
                src[sha(line)] = f"{target}:{line[:60]}"
        db = {
            r["raw_hash"]: r["status"]
            for r in conn.execute(
                "SELECT raw_hash,status FROM facts WHERE profile=? AND origin='l0'", (bank,)
            )
        }
        return {
            "bank": bank,
            "source_lines": len(src),
            "db_rows": len(db),
            "only_source": [v for h, v in src.items() if h not in db][:20],
            "only_db_active": [h for h, st in db.items() if h not in src and st == "active"][:20],
            "archived_in_db": sum(1 for st in db.values() if st == "archived"),
        }
    finally:
        conn.close()


# ---------------- INDEX.md 倒排索引（2026-11-26 收编自 scripts/update_index.py） ----------------

INDEX_FILE = paths.INDEX_FILE
INDEX_SKIP_DIRS = {"sessions"}
INDEX_NOISE = {
    "配置", "方法", "系统", "使用", "需要", "可以", "相关", "说明", "详情", "注意",
    "这个", "那个", "什么", "怎么", "如何", "是否", "没有", "不是", "就是", "一个",
    "已经", "不能", "我们的", "以及", "并且", "或者", "因为", "所以", "但是", "然后",
    "文件", "目录", "路径", "脚本", "工具", "功能", "问题", "解决", "修复", "更新",
    "完成", "Pro", "Max", "Plus", "Mini", "Ultra",
}


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2]
    return text


def _fm_tags(text: str) -> list:
    """只取 front-matter 的 tags（人工标注最精准；其余字段与本索引无关）。"""
    if not text.startswith("---"):
        return []
    parts = text.split("---", 2)
    if len(parts) != 3:
        return []
    for line in parts[1].splitlines():
        line = line.strip()
        if line.startswith("tags:"):
            raw = line.split(":", 1)[1].strip().strip("[]").replace(" ", "")
            return [x for x in raw.split(",") if x]
    return []


def _index_title(body: str):
    """H1 优先；无 H1 时退第一个 H2（与 indexer 的条目口径对齐）；再退 None。"""
    h2 = None
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# ") and not s.startswith("## "):
            return s[2:].strip()
        if h2 is None and s.startswith("## "):
            h2 = s[3:].strip()
    return h2


def _kw_from_text(text: str, limit: int = 8) -> list:
    words = set()
    words.update(re.findall(r"(?<![\u4e00-\u9fff])[\u4e00-\u9fff]{2}(?![\u4e00-\u9fff])", text))
    words.update(re.findall(r"(?<![a-zA-Z0-9_.-])[a-zA-Z]+\d+(?:-\d+)*(?![a-zA-Z0-9_.-])", text))
    words.update(re.findall(r"(?<![a-zA-Z0-9_.-])[a-zA-Z]{2,}(?![a-zA-Z0-9_.-])", text))
    # 必须排序：set 迭代顺序随 PYTHONHASHSEED 变化，否则每次生成的 INDEX 都不同（内容比对/check 永远不一致）
    picked = sorted(w for w in words if w not in INDEX_NOISE and len(w) >= 2)
    return picked[:limit]


def build_index_text() -> tuple:
    """返回 (index_text, file_count)。扫描 memory/ 下所有非隐藏、非 sessions 目录（保持原口径）。"""
    index: dict = {}
    files = 0
    if not MEMORY_DIR.is_dir():
        return "", 0
    for bank_dir in sorted(MEMORY_DIR.iterdir()):
        if not bank_dir.is_dir() or bank_dir.name.startswith(".") or bank_dir.name in INDEX_SKIP_DIRS:
            continue
        for f in sorted(bank_dir.glob("*.md")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            files += 1
            body = _strip_frontmatter(text)
            title = _index_title(body) or f.name
            label = f"{bank_dir.name}/{f.name}: {title}"
            keywords = _fm_tags(text) + _kw_from_text(title, limit=4)
            seen = set()
            for kw in keywords:
                k = kw.lower()
                if k in seen or not kw:
                    continue
                seen.add(k)
                index.setdefault(kw, []).append(label)
    lines = [f"{kw} → {'，'.join(dict.fromkeys(index[kw]))}"
             for kw in sorted(index.keys(), key=lambda x: -len(x))]
    return "\n".join(lines), files


def rebuild_index_file(check_only: bool = False) -> dict:
    """重建 INDEX.md（MEMD_INDEX_FILE）。内容未变则不写盘（避免每轮索引都触发 mtime 变化）。"""
    text, files = build_index_text()
    header = ("# 记忆库索引（自动生成，勿手改）\n\n"
              "> 由 memd index 自动维护（2026-11-26 收编自 update_index.py）。每个条目 = 一个文件（文件级原子）。\n\n---\n\n")
    new_full = header + text + "\n"
    old = INDEX_FILE.read_text(encoding="utf-8", errors="replace") if INDEX_FILE.exists() else ""
    changed = old != new_full
    if check_only:
        return {"check_only": True, "changed": changed, "files": files, "keywords": len(text.splitlines())}
    if changed:
        INDEX_FILE.write_text(new_full, encoding="utf-8")
    return {"rebuilt": changed, "files": files, "keywords": len(text.splitlines()), "path": str(INDEX_FILE)}


if __name__ == "__main__":
    import sys

    print(json.dumps(index_all(), ensure_ascii=False, indent=2))
