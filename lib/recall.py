#!/usr/bin/env python3
"""星忆召回：BM25 + 向量 + 别名 + 时间，RRF 归一化，乘性重排，token 预算。

零 numpy 依赖：105 条 × 512 维的点积用纯 Python，毫秒级。
向量模型从 <MEMD_BASE>/venvs/emb 借用（fastembed + bge-small-zh）；无则自动 BM25-only。
"""
from __future__ import annotations

import glob
import json
import math
import pickle
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paths  # noqa: E402  路径单一入口：改环境变量即可迁移

ROOT = paths.ROOT
BANKS_DIR = ROOT / "data" / "banks"
VEC_DIR = ROOT / "data" / "vectors"
ALIAS_FILE = ROOT / "data" / "aliases.json"
MEMORY_DIR = paths.MEMORY_DIR
TZ = timezone(timedelta(hours=8))
BANKS = paths.BANKS
RRF_K = 60
LANES = 2  # 只有 BM25 与向量产生排名列表

sys.path.insert(0, str(Path(__file__).parent))
import indexer  # noqa: E402

_EMB_SITE = glob.glob(paths.EMB_SITE_GLOB)
if _EMB_SITE:
    sys.path.insert(0, _EMB_SITE[0])
_MODEL = None
_MODEL_ERR = ""


def model():
    global _MODEL, _MODEL_ERR
    if _MODEL is None and not _MODEL_ERR:
        try:
            from fastembed import TextEmbedding

            _MODEL = TextEmbedding(
                model_name="BAAI/bge-small-zh-v1.5",
                cache_dir=str(paths.FASTEMBED_CACHE),
            )
        except Exception as exc:  # 向量不可用 → BM25-only 降级
            _MODEL_ERR = f"{type(exc).__name__}: {exc}"
    return _MODEL


def connect(bank: str) -> sqlite3.Connection:
    conn = sqlite3.connect(BANKS_DIR / f"{bank}.db", timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _vec_path(bank: str) -> Path:
    return VEC_DIR / f"{bank}.pkl"


def build_vectors(bank: str) -> dict:
    conn = connect(bank)
    rows = conn.execute(
        "SELECT id,text FROM facts WHERE profile=? AND status='active' AND recallable=1", (bank,)
    ).fetchall()
    conn.close()
    if not rows:
        return {"bank": bank, "vectors": 0, "degraded": "no_facts"}
    m = model()
    if m is None:
        return {"bank": bank, "vectors": 0, "degraded": _MODEL_ERR}
    texts = [r["text"][:800] for r in rows]
    vecs = [list(map(float, v)) for v in m.embed(texts)]
    VEC_DIR.mkdir(parents=True, exist_ok=True)
    with _vec_path(bank).open("wb") as f:
        pickle.dump({"ids": [r["id"] for r in rows], "vecs": vecs}, f)
    return {"bank": bank, "vectors": len(vecs), "degraded": ""}


def _load_vectors(bank: str):
    p = _vec_path(bank)
    if not p.exists():
        return None
    try:
        with p.open("rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v)) or 1.0


def _aliases() -> dict:
    try:
        return json.loads(ALIAS_FILE.read_text(encoding="utf-8")) if ALIAS_FILE.exists() else {}
    except Exception:
        return {}


def expand(query: str) -> list[str]:
    """别名扩展：命中键就带上扩展词，扩展词只加分不单独成路。"""
    out = []
    for k, vs in _aliases().items():
        if k and k in query:
            out.extend(vs if isinstance(vs, list) else [str(vs)])
    return out


_TIME_RULES = [
    (r"今天|今日", 0),
    (r"昨天|昨日", 1),
    (r"前天", 2),
]


def time_window(query: str):
    now = datetime.now(TZ)
    for pat, days in _TIME_RULES:
        if re.search(pat, query):
            start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            return start, end
    m = re.search(r"(\d{4})[-年](\d{1,2})", query)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        start = datetime(y, mo, 1, tzinfo=TZ)
        end = datetime(y + (mo == 12), 1 if mo == 12 else mo + 1, 1, tzinfo=TZ)
        return start, end
    if re.search(r"上周", query):
        start = (now - timedelta(days=now.weekday() + 7)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=7)
    return None


_STOP = {
    "什么", "怎么", "如何", "哪个", "哪里", "为什么", "可以", "一下", "我们", "这个",
    "那个", "是不", "不是", "是什", "么原", "原因", "多少", "哪个", "是否", "怎样",
}


def _bm25_ranks(bank: str, query: str) -> list[str]:
    seg = indexer.segment(query).replace('"', "").strip()
    if not seg:
        return []
    toks = [
        t for t in seg.split()
        if t not in _STOP and (len(t) >= 3 or (len(t) == 2 and all("\u4e00" <= c <= "\u9fff" for c in t)))
    ]
    if not toks:
        return []
    expr = " OR ".join('"' + t + '"' for t in toks)
    conn = connect(bank)
    try:
        rows = conn.execute(
            "SELECT f.id FROM facts_fts t JOIN facts f ON f.id=t.fact_id "
            "WHERE facts_fts MATCH ? AND f.status='active' AND f.recallable=1 "
            "ORDER BY bm25(facts_fts) LIMIT 50",
            (expr,),
        ).fetchall()
        return [r["id"] for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _vec_ranks(bank: str, query: str) -> list[str]:
    data = _load_vectors(bank)
    m = model()
    if not data or m is None:
        return []
    qv = list(map(float, list(m.embed([query]))[0]))
    qn = _norm(qv)
    scored = []
    for fid, v in zip(data["ids"], data["vecs"]):
        cos = _dot(qv, v) / (qn * _norm(v) or 1.0)
        if cos >= 0.45:  # 绝对相关性下限：噪声基线约 0.33-0.44
            scored.append((cos, fid))
    scored.sort(reverse=True)
    return [fid for _, fid in scored[:30]]


def _rrf(ranks_lists: list[list[str]]) -> dict[str, float]:
    fused: dict[str, float] = {}
    for ranks in ranks_lists:
        for rank, fid in enumerate(ranks, 1):
            fused[fid] = fused.get(fid, 0.0) + 1.0 / (RRF_K + rank)
    return fused


def _reindex_file(conn, bank: str, path: str) -> bool:
    """一次到位：索引与源文件不一致时，当场重索引该文件（单文件增量）。"""
    try:
        run_id = f"live-{datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}"
        indexer.index_file(conn, bank, Path(path), run_id)
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_source(conn, bank: str, row) -> tuple[str | None, str]:
    """回源取正文：文件类条目只存索引，正文在文件里。

    返回 (text, degraded)。失败原因：no_source_path / source_missing /
    entry_missing / line_missing。
    """
    path = row["source_path"]
    if not path:
        return None, "no_source_path"
    p = Path(path)
    if not p.is_file():
        return None, "source_missing"
    raw = p.read_text(encoding="utf-8", errors="replace")
    origin = row["origin"]

    def _from_file() -> tuple[str | None, str]:
        if origin == "l1":
            title = row["subject"] or ""
            if not title:
                return None, "entry_missing"
            m = re.search(rf"^##\s+{re.escape(title)}\s*$", raw, re.M)
            if not m:
                return None, "entry_missing"
            body = raw[m.end():]
            nxt = re.search(r"^##\s+", body, re.M)
            return (body[:nxt.start()] if nxt else body).strip(), ""
        if origin == "l0":
            h = row["raw_hash"]
            for line in raw.splitlines():
                if line.strip() and indexer.sha(line.strip()) == h:
                    return line.strip(), ""
            return None, "line_missing"
        return None, "unsupported_origin"

    text, why = _from_file()
    if text is not None:
        return text, ""
    # 一次到位：文件在但条目找不到 → 当场重索引再试一次
    if why in ("entry_missing", "line_missing") and _reindex_file(conn, bank, path):
        row2 = conn.execute(
            "SELECT id,text,origin,status,learned_at,domain,subject,source_path,raw_hash "
            "FROM facts WHERE id=?", (row["id"],)
        ).fetchone()
        if row2 is not None:
            raw2 = Path(path).read_text(encoding="utf-8", errors="replace")
            if row2["origin"] == "l1":
                m = re.search(rf"^##\s+{re.escape(row2['subject'] or '')}\s*$", raw2, re.M)
                if m:
                    body = raw2[m.end():]
                    nxt = re.search(r"^##\s+", body, re.M)
                    return (body[:nxt.start()] if nxt else body).strip(), ""
            else:
                for line in raw2.splitlines():
                    if line.strip() and indexer.sha(line.strip()) == row2["raw_hash"]:
                        return line.strip(), ""
    return None, why


def recall(bank: str, query: str, budget_tokens: int = 400, max_items: int = 2,
           min_score: float = 0.35) -> dict:
    query = (query or "").strip()[:800]
    if not query:
        return {"bank": bank, "query": query, "items": [], "tokens": 0, "degraded": "empty_query"}
    if query in {"继续", "好", "好的", "嗯", "行", "可以", "收到", "明白", "谢谢",
                 "在吗", "测试", "ok", "OK", "嗯嗯", "好嘞", "了解"}:
        return {"bank": bank, "query": query, "items": [], "tokens": 0, "degraded": "trivial"}

    expanded = expand(query)
    q = query + (" " + " ".join(expanded) if expanded else "")
    lanes = []
    bm = _bm25_ranks(bank, q)
    if bm:
        lanes.append(bm)
    vr = _vec_ranks(bank, q)
    if vr:
        lanes.append(vr)
    if not lanes:
        return {"bank": bank, "query": query, "items": [], "tokens": 0, "degraded": "no_lane_hit"}

    fused = _rrf(lanes)
    denom = max(fused.values()) if fused else 1.0  # 按本次最高分归一
    conn = connect(bank)
    try:
        win = time_window(query)
        q_tokens = set(indexer.segment(query).split())
        items = []
        for fid, raw in fused.items():
            row = conn.execute(
                "SELECT id,text,origin,status,learned_at,domain,subject,source_path,raw_hash "
                "FROM facts WHERE id=?", (fid,)
            ).fetchone()
            if row is None or row["status"] not in ("active",):
                continue
            if win:
                try:
                    ts = datetime.fromisoformat(row["learned_at"])
                    if not (win[0] <= ts < win[1]):
                        continue
                except Exception:
                    pass
            score = raw / denom
            title = row["subject"] or ""
            if q_tokens and any(t in title for t in q_tokens if len(t) >= 2):
                score *= 1.20
            if row["origin"] == "l1":
                score *= 1.05
            try:
                if (datetime.now(TZ) - datetime.fromisoformat(row["learned_at"])).days <= 7:
                    score *= 1.10
            except Exception:
                pass
            if score < min_score:
                continue
            text = row["text"]
            if not text:
                # 库不存文件类正文：回源读取（失败则本轮丢弃，信号层会记 l1_missing）
                text, _why = _load_source(conn, bank, row)
                if not text:
                    continue
            snippet = re.sub(r"\s+", " ", text)
            if title and snippet.startswith(title):
                snippet = snippet[len(title):].lstrip(" ：:")
            snippet = snippet[:120]
            items.append({
                "fact_id": fid,
                "title": title or row["domain"] or "",
                "snippet": snippet,
                "score": round(score, 4),
                "origin": row["origin"],
                "learned_at": row["learned_at"],
            })
    finally:
        conn.close()

    items.sort(key=lambda x: -x["score"])
    picked, used = [], 0
    for it in items:
        cost = len(it["title"]) + len(it["snippet"]) + 8
        if used + cost > budget_tokens * 2.75:  # 中文约 2.75 字符/token
            continue
        picked.append(it)
        used += cost
        if len(picked) >= max_items:
            break
    return {
        "bank": bank,
        "query": query,
        "items": picked,
        "tokens": round(used / 2.75),
        "chars": used,
        "lanes": {"bm25": len(bm), "vector": len(vr)},
        "degraded": "" if vr else "vector_unavailable",
    }
