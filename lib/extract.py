#!/usr/bin/env python3
"""星忆 S3b：对话抽取（规则后端）。

设计原则：
- 默认规则后端，零 LLM、零凭据；LLM 后端留接口，凭据治理后再接
- 规则/偏好白名单式排除：模态词直接拒抽，与 L0 高相似也拒抽
- 每条事实带 quote 原文锚点，幂等键 (session, turn, extractor_version)
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paths  # noqa: E402  路径单一入口：改环境变量即可迁移

ROOT = paths.ROOT
BANKS_DIR = ROOT / "data" / "banks"
TZ = timezone(timedelta(hours=8))
EXTRACTOR_VERSION = "rule-1"
MAX_PER_TURN = 5
MAX_SENTENCE = 160
MIN_SENTENCE = 12

# 只留「带具体值」的结论：路径/IP/版本/端口/键名
VALUE = re.compile(
    r"(/[\w./-]{2,}|\b\d{1,3}(?:\.\d{1,3}){3}\b|v?\d+\.\d+(?:\.\d+)?|:\d{4,5}\b|<见加密库)"
)
# 结论词必须同时带数字，才算实测结论
VERDICT = re.compile(r"(实测|确认|定案|结论|根因|默认)")
# 规则 / 偏好 / 过程 / 对话：一律不进自动抽取
BLOCK = re.compile(
    r"(必须|不要|一律|永远|以后都|优先|宁可|禁止|红线|铁律|偏好|喜欢|不喜欢|习惯|务必|"
    r"还没|正在|下一步|修法|方案|建议|应该|可以|需要|是否|如果|要是|要现在|请主人|等你|"
    r"我先|我会|我去|刚才|现在|准备|打算|继续|所以|因为|但是|然后|接着|实现|完成|待办|"
    r"阻断|重要|建议|喵|吗|呢|→|\"fix\")"
)
# 明显不是事实的噪声
NOISE = re.compile(r"(^\s*[|>`#\-*\d]|https?://|```|\{|\}|def |class |import |[?？]\s*$)")


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _grams(text: str, n: int = 3) -> set[str]:
    t = re.sub(r"\s+", "", text)
    return {t[i : i + n] for i in range(max(0, len(t) - n + 1))}


def jaccard(a: str, b: str) -> float:
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])|\n+", text or "")
    return [p.strip(" -•\t") for p in parts if p and p.strip()]


def extract(text: str, l0_lines: list[str]) -> list[dict]:
    out: list[dict] = []
    for s in _sentences(text):
        if len(s) < MIN_SENTENCE or len(s) > MAX_SENTENCE:
            continue
        if NOISE.search(s) or BLOCK.search(s):
            continue
        # 必须带具体值（路径/IP/版本/端口/键名）；VERDICT 只作辅助，不能单独放行
        if not VALUE.search(s):
            continue
        if l0_lines and max(jaccard(s, l) for l in l0_lines) >= 0.85:
            continue
        out.append({"fact": s, "quote": s})
        if len(out) >= MAX_PER_TURN:
            break
    return out


def connect(bank: str) -> sqlite3.Connection:
    conn = sqlite3.connect(BANKS_DIR / f"{bank}.db", timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_queue(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS turn_queue(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             profile TEXT, session_id TEXT, turn_id TEXT,
             extractor_version TEXT NOT NULL DEFAULT 'rule-1',
             user_text TEXT, assistant_text TEXT,
             status TEXT DEFAULT 'pending', reason TEXT,
             created_at TEXT, processed_at TEXT,
             UNIQUE(session_id, turn_id, extractor_version))"""
    )


def l0_lines(bank: str) -> list[str]:
    import indexer

    out: list[str] = []
    for path, _target in indexer.l0_paths(bank):
        out.extend(indexer._l0_lines(path))
    return out


def enqueue(bank: str, session_id: str, turn_id: str, user: str, assistant: str,
            queue_cap: int = 500, daily_budget: int = 500) -> dict:
    conn = connect(bank)
    try:
        ensure_queue(conn)
        today = now()[:10]
        done_today = conn.execute(
            "SELECT count(*) FROM turn_queue WHERE status='done' AND processed_at LIKE ?",
            (today + "%",),
        ).fetchone()[0]
        if done_today >= daily_budget:
            conn.commit()
            return {"queued": False, "reason": "daily_budget"}
        cur = conn.execute(
            "INSERT OR IGNORE INTO turn_queue(profile,session_id,turn_id,extractor_version,user_text,assistant_text,status,created_at) "
            "VALUES(?,?,?,?,?,?,'pending',?)",
            (bank, session_id, turn_id, EXTRACTOR_VERSION, user[:4000], assistant[:4000], now()),
        )
        inserted = cur.rowcount > 0
        depth = conn.execute("SELECT count(*) FROM turn_queue WHERE status='pending'").fetchone()[0]
        if depth > queue_cap:
            conn.execute(
                "UPDATE turn_queue SET status='dropped', reason='queue_full' "
                "WHERE id IN (SELECT id FROM turn_queue WHERE status='pending' ORDER BY id LIMIT ?)",
                (depth - queue_cap,),
            )
        conn.commit()
        return {"queued": inserted, "depth": depth, "budget": daily_budget - done_today}
    finally:
        conn.close()


def process_pending(bank: str, limit: int = 20) -> dict:
    conn = connect(bank)
    stats = {"done": 0, "facts": 0, "skipped": 0}
    try:
        ensure_queue(conn)
        # claim：先抢占再处理，worker 与 CLI 并发时不会重复抽同一行
        claimed = conn.execute(
            "UPDATE turn_queue SET status='processing', processed_at=? "
            "WHERE id IN (SELECT id FROM turn_queue WHERE status='pending' ORDER BY id LIMIT ?) "
            "RETURNING id,session_id,turn_id,user_text,assistant_text",
            (now(), limit),
        ).fetchall()
        conn.commit()
        if not claimed:
            return stats
        l0 = l0_lines(bank)
        for r in claimed:
            try:
                text = (r["user_text"] or "") + "\n" + (r["assistant_text"] or "")
                items = extract(text, l0)
                if not items:
                    conn.execute(
                        "UPDATE turn_queue SET status='skipped', reason='no_fact', processed_at=? WHERE id=?",
                        (now(), r["id"]),
                    )
                    stats["skipped"] += 1
                    continue
                for it in items:
                    fact = it["fact"]
                    canonical = sha(re.sub(r"\s+", "", fact))[:16]
                    fact_id = sha(f"{bank}|turn|{canonical}")[:32]
                    conn.execute(
                        """INSERT INTO facts(id,profile,domain,scope,canonical_key,value,
                             visibility,target,text,origin,learned_at,status,recallable,no_consolidate,raw_hash,hash)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(profile,canonical_key) DO UPDATE SET
                             learned_at=excluded.learned_at, status='active', deleted_at=NULL""",
                        (
                            fact_id, bank, "turn", f"{bank}/turn", canonical, fact,
                            "private", "memory", fact, "turn", now(), "active", 1, 0,
                            sha(fact), sha(fact),
                        ),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO fact_sources(fact_id,source_kind,session_id,turn_id,quote,quote_hash,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (fact_id, "turn", r["session_id"], r["turn_id"], it["quote"][:500],
                         sha(it["quote"][:500]), now()),
                    )
                    import indexer as _idx

                    conn.execute("DELETE FROM facts_fts WHERE fact_id=?", (fact_id,))
                    conn.execute(
                        "INSERT INTO facts_fts(fact_id,text_seg) VALUES(?,?)",
                        (fact_id, _idx.segment(fact)),
                    )
                    stats["facts"] += 1
                conn.execute(
                    "UPDATE turn_queue SET status='done', reason=NULL, processed_at=? WHERE id=?",
                    (now(), r["id"]),
                )
                stats["done"] += 1
            except Exception as exc:
                conn.execute(
                    "UPDATE turn_queue SET status='pending', reason=? WHERE id=?",
                    (f"retry:{type(exc).__name__}", r["id"]),
                )
        conn.commit()
        return stats
    finally:
        conn.close()


def start_worker(banks: tuple[str, ...], interval: float = 3.0) -> threading.Thread:
    def _loop() -> None:
        while True:
            for b in banks:
                try:
                    process_pending(b)
                except Exception as exc:
                    print(f"turn worker {b}: {exc}", flush=True)
            time.sleep(interval)

    t = threading.Thread(target=_loop, name="starmem-turn-worker", daemon=True)
    t.start()
    return t
