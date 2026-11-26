#!/usr/bin/env python3
"""星忆 · 信号（signals）：环境里哪些变化成立、该做什么。

判定归星忆，注入归 Hermes env-reminder。信号是「工单」不是「提醒」：
每条带 evidence / target / action / template，agent 处理后回写 done。

已实现判据：
  l0_pressure  L0 占用 ≥90%（候选：最长条目）
  l1_missing   地图/INDEX 指向的 L1 文件不存在

待观察（先不生成，避免误报）：
  l1_unfiled   新 domain 未归档 —— 判据需一周数据定阈值
  l1_extend    已有 domain 新维度 —— 同上

表：
  signals(id, bank, type, severity, evidence, target, action, template,
          status, created_at, closed_at, result, remind_count)
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
import paths  # noqa: E402  路径单一入口

BANKS_DIR = paths.BANKS_DIR
MEMORY_DIR = paths.MEMORY_DIR
STATE_DIR = paths.STATE_DIR
L0_HOMES = paths.l0_homes()
INDEX = paths.INDEX_FILE
PRESSURE = STATE_DIR / "memory_pressure"
L0_TRIGGER = 90.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals(
  id TEXT PRIMARY KEY, bank TEXT, type TEXT, severity TEXT,
  evidence TEXT, target TEXT, action TEXT, template TEXT,
  status TEXT DEFAULT 'open', created_at TEXT, closed_at TEXT,
  result TEXT, remind_count INTEGER DEFAULT 0,
  UNIQUE(bank, type, target)
);
"""


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def connect(bank: str) -> sqlite3.Connection:
    p = BANKS_DIR / f"{bank}.db"
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def _sig_id(bank: str, typ: str, target: str) -> str:
    import hashlib
    h = hashlib.sha1(f"{bank}|{typ}|{target}".encode()).hexdigest()[:10]
    return f"sig-{typ[:3]}-{h}"


def _upsert(conn, bank, typ, severity, evidence, target, action, template="") -> bool:
    """同一 (bank,type,target) 已 open 则只累加提醒次数；closed 的重新开一张。"""
    sid = _sig_id(bank, typ, target)
    row = conn.execute("SELECT status, remind_count, closed_at FROM signals WHERE id=?", (sid,)).fetchone()
    if row:
        if row["status"] == "open":
            conn.execute("UPDATE signals SET remind_count=remind_count+1, severity=? WHERE id=?",
                         (severity, sid))
            return False
        # 已关闭：抑制期内不重开（默认 24h），防「关掉立刻又冒出来」
        closed = row["closed_at"] or ""
        if closed:
            try:
                if datetime.now(TZ) - datetime.fromisoformat(closed) < timedelta(hours=24):
                    return False
            except Exception:  # noqa: BLE001
                pass
    conn.execute(
        "INSERT OR REPLACE INTO signals(id,bank,type,severity,evidence,target,action,template,"
        "status,created_at,closed_at,result,remind_count) VALUES(?,?,?,?,?,?,?,?,'open',?,NULL,NULL,1)",
        (sid, bank, typ, severity, evidence, target, action, template, now()),
    )
    return True


def _l0_lines(bank: str) -> list[str]:
    home = L0_HOMES.get(bank)
    if not home:
        return []
    p = home / "memories" / "MEMORY.md"
    if not p.exists():
        return []
    out = []
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line in ("§", "---") or line.startswith("```"):
            continue
        out.append(line)
    return out


def scan(bank: str) -> list[dict]:
    """生成/更新信号，返回当前 open 列表。"""
    conn = connect(bank)
    created = []
    try:
        # ① l0_pressure
        pf = PRESSURE / f"{bank}.json"
        if pf.exists():
            try:
                d = json.loads(pf.read_text(encoding="utf-8"))
                pct = float(d.get("pct") or 0)
                if pct >= L0_TRIGGER:
                    lines = _l0_lines(bank)
                    longest = sorted(lines, key=len, reverse=True)[:5]
                    ev = (f"L0 {pct:.1f}%（{d.get('chars')}/{d.get('limit')}）；"
                          f"最长 5 条：" + " | ".join(f"{len(x)}字:{x[:28]}" for x in longest))
                    if _upsert(conn, bank, "l0_pressure", "action", ev,
                               f"{bank}/MEMORY.md",
                               "按规范压缩：合并同主题 → 长条目下沉 L1 留一行指针 → 删过期",
                               "写库/写文件标准见 memory-management 的 type×tier"):
                        created.append("l0_pressure")
            except Exception:  # noqa: BLE001  容量文件损坏不影响其他判据
                pass

        # ② l1_missing（地图与 INDEX 指向但不存在的文件）
        missing: dict[str, str] = {}
        srcs = []
        home = L0_HOMES.get(bank)
        if home:
            lp = home / "memories" / "MEMORY.md"
            if lp.exists():
                srcs.append((str(lp), lp.read_text(encoding="utf-8", errors="replace")))
        if INDEX.exists():
            srcs.append((str(INDEX), INDEX.read_text(encoding="utf-8", errors="replace")))
        for src_path, text in srcs:
            # 兼容两种引用写法：MEMORY.md 的 `memory/<bank>/x.md`、INDEX.md 的 `<bank>/x.md`
            for m in re.finditer(rf"(?:memory/)?{bank}/([A-Za-z0-9_\-]+\.md)", text):
                rel = f"memory/{bank}/{m.group(1)}"
                p = paths.BASE / rel
                if not p.exists():
                    missing[rel] = src_path
        for rel, src in missing.items():
            if _upsert(conn, bank, "l1_missing", "action",
                       f"引用自 {src}；文件不存在：{paths.BASE}/{rel}",
                       rel,
                       "补写正文（按 domain 建文件）或删除悬空指针（二选一）",
                       "L1 文件需 front-matter：type/tier"):
                created.append(f"l1_missing:{rel}")

        conn.commit()
        rows = conn.execute(
            "SELECT id,type,severity,evidence,target,action,remind_count,created_at "
            "FROM signals WHERE bank=? AND status='open' ORDER BY "
            "CASE severity WHEN 'action' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, created_at",
            (bank,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def close(sig_id: str, status: str = "done", result: str = "") -> dict:
    for b in ("default", "maojiu", "lingyuan", "shared"):
        p = BANKS_DIR / f"{b}.db"
        if not p.exists():
            continue
        conn = sqlite3.connect(p)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(SCHEMA)
            row = conn.execute("SELECT id,bank FROM signals WHERE id=?", (sig_id,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE signals SET status=?, closed_at=?, result=? WHERE id=?",
                    (status, now(), result[:500], sig_id),
                )
                conn.commit()
                return {"ok": True, "id": sig_id, "bank": row["bank"], "status": status}
        finally:
            conn.close()
    return {"ok": False, "error": "not_found", "id": sig_id}


def list_open(bank: str) -> list[dict]:
    return scan(bank)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="星忆信号：扫描/列出/关闭")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("--bank", default="default")
    c = sub.add_parser("close"); c.add_argument("id")
    c.add_argument("--status", default="done"); c.add_argument("--result", default="")
    a = ap.parse_args()
    if a.cmd == "scan":
        print(json.dumps(list_open(a.bank), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(close(a.id, a.status, a.result), ensure_ascii=False))
