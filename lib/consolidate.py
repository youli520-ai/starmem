#!/usr/bin/env python3
"""星忆 S4：规则整合（不调 LLM）。

输入：active、no_consolidate=0、未整合的 facts（turn/l1）。
输出：proposals/<bank>-<ts>.md 提案，人工审后再落 L1。
只做聚类与矛盾候选标注，不自动改写。
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import paths  # noqa: E402  路径单一入口：改环境变量即可迁移

ROOT = paths.ROOT
BANKS_DIR = ROOT / "data" / "banks"
PROPOSAL_DIR = ROOT / "proposals"
TZ = timezone(timedelta(hours=8))


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def connect(bank: str) -> sqlite3.Connection:
    conn = sqlite3.connect(BANKS_DIR / f"{bank}.db", timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _key(text: str) -> str:
    return re.sub(r"\s+", "", text)[:60]


def consolidate(bank: str, limit: int = 200) -> dict:
    conn = connect(bank)
    try:
        rows = conn.execute(
            "SELECT id,subject,predicate,topic,value,text,domain,origin,learned_at "
            "FROM facts WHERE profile=? AND status='active' AND no_consolidate=0 "
            "AND consolidated_at IS NULL AND origin='turn' "
            "ORDER BY domain, learned_at LIMIT ?",
            (bank, limit),
        ).fetchall()
        if not rows:
            return {"bank": bank, "facts": 0, "groups": 0, "proposal": ""}

        groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            groups[r["domain"] or "misc"].append(r)

        # 矛盾候选：同 subject+predicate 不同 value
        by_pred: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            if r["subject"] and r["predicate"]:
                by_pred[(r["subject"], r["predicate"])].append(r)
        conflicts = []
        for (subj, pred), items in by_pred.items():
            values = {_key(i["value"] or i["text"]) for i in items}
            if len(values) > 1:
                conflicts.append((subj, pred, items))

        PROPOSAL_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
        path = PROPOSAL_DIR / f"{bank}-{ts}.md"
        lines = [
            f"# 星忆整合提案 · {bank}",
            "",
            f"> 生成：{now()}｜事实 {len(rows)} 条｜分组 {len(groups)}｜矛盾候选 {len(conflicts)}",
            "> 人工审后落 L1；本提案不自动改写任何文件。",
            "",
        ]
        for domain, items in sorted(groups.items()):
            lines.append(f"## {domain}（{len(items)}）")
            for r in items:
                snippet = re.sub(r"\s+", " ", r["text"])[:160]
                lines.append(f"- `{r['origin']}` {snippet}")
            lines.append("")
        if conflicts:
            lines.append("## ⚠️ 矛盾候选")
            for subj, pred, items in conflicts:
                lines.append(f"### {subj} / {pred}")
                for r in items:
                    lines.append(f"- [{r['learned_at']}] {re.sub(r'\\s+', ' ', r['text'])[:160]}")
                lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")

        ids = [r["id"] for r in rows]
        conn.execute(
            f"UPDATE facts SET consolidated_at=? WHERE id IN ({','.join('?' * len(ids))})",
            (now(), *ids),
        )
        conn.commit()
        return {
            "bank": bank,
            "facts": len(rows),
            "groups": len(groups),
            "conflicts": len(conflicts),
            "proposal": str(path),
        }
    finally:
        conn.close()
