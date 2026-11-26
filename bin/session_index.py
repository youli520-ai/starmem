#!/usr/bin/env python3
"""星忆 · 会话消息级索引（与旧 semantic_recall build3 口径等价）。

为什么需要：星忆 sessions 表是会话级摘要，只能定位到「哪个会话」；
旧 semantic_recall 用消息级向量 + 会话聚合，能按模糊描述找回具体一轮。
本脚本把旧口径搬进星忆，保证替换后能力不降级。

口径（必须与 semantic_recall_pilot.py 的 _policy_snapshot 一致）：
  sessions.source IN ('feishu','tui')
  且 (role='user' OR (role='assistant' AND length(content)>80))
  且 content NOT LIKE '[System:%'
文本：user → '问：' + content[:300]；assistant → '答：' + content[:300]
标题：'会话（YYYY-MM-DD）：<title>。<aliases>'
模型：BAAI/bge-small-zh-v1.5（与旧索引同档位，维度必须一致）

产物：<MEMD_ROOT>/data/vectors/session_<bank>.npz + .manifest.json
用法：
  session_index.py build --bank default [--full]
  session_index.py query --bank default -k 5 "模糊描述"
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import paths as sm_paths  # noqa: E402  路径单一入口（别名：本文件另有同名函数 paths()）

ROOT = sm_paths.ROOT
VEC_DIR = ROOT / "data" / "vectors"
STATE_DB = {b: sm_paths.state_db(b) for b in ("default", "maojiu", "lingyuan")}
ALIASES = sm_paths.ALIASES
TZ = dt.timezone(dt.timedelta(hours=8))
MODEL = "BAAI/bge-small-zh-v1.5"
# 查询侧必须与旧索引同一条检索指令，否则向量空间错位（实测 0.66 分数全是无关会话）
INSTRUCT = "为这个句子生成表示以用于检索相关文章：{q}"
CORPUS_POLICY_VERSION = "v3.dual.2026-11-24"
SCHEMA_VERSION = 1

_MODEL = None


def model():
    global _MODEL
    if _MODEL is None:
        from fastembed import TextEmbedding  # noqa: PLC0415
        # 修复 B2：显式钉住模型缓存目录（默认写 ~/.cache，属 root 不可写 → Permission denied）
        _MODEL = TextEmbedding(
            model_name=MODEL,
            threads=int(os.environ.get("EMB_THREADS", "2")),
            cache_dir=os.environ.get("FASTEMBED_CACHE") or str(sm_paths.FASTEMBED_CACHE),
        )
    return _MODEL


def normalize(arr):
    import numpy as np  # noqa: PLC0415
    arr = np.asarray(arr, dtype="float32")
    return arr / (np.linalg.norm(arr, axis=-1, keepdims=True) + 1e-9)


def paths(bank: str) -> tuple[Path, Path]:
    return VEC_DIR / f"session_{bank}.npz", VEC_DIR / f"session_{bank}.npz.manifest.json"


def policy_rows(db: sqlite3.Connection):
    """与旧索引完全相同的语料快照。"""
    max_all = db.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 0
    sess = db.execute(
        "SELECT id, title, started_at FROM sessions "
        "WHERE source IN ('feishu','tui') ORDER BY started_at"
    ).fetchall()
    rows = db.execute(
        "SELECT m.id, m.session_id, m.role, m.content FROM messages m "
        "JOIN sessions s ON m.session_id=s.id "
        "WHERE s.source IN ('feishu','tui') "
        "AND (m.role='user' OR (m.role='assistant' AND length(m.content)>80)) "
        "AND m.content NOT LIKE '[System:%' "
        "ORDER BY m.id"
    ).fetchall()
    return sess, rows, max_all


def range_hash(rows) -> str:
    h = hashlib.sha256()
    for mid, sid, role, content in rows:
        c = content or ""
        h.update(f"{mid}|{len(c)}|{hashlib.md5(c[:300].encode()).hexdigest()}\n".encode())
    return h.hexdigest()


def title_texts(sess) -> list[str]:
    aliases = {}
    if ALIASES.exists():
        try:
            aliases = json.loads(ALIASES.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            aliases = {}
    out = []
    for sid, title, started in sess:
        date = dt.datetime.fromtimestamp(started, TZ).strftime("%Y-%m-%d")
        extra = " ".join(
            " ".join(v) if isinstance(v, list) else str(v)
            for k, v in aliases.items() if str(sid).startswith(k)
        )
        out.append(f"会话（{date}）：{title}。{extra}")
    return out


def build(bank: str, full: bool = False, seed_from: str | None = None) -> int:
    import numpy as np  # noqa: PLC0415

    src = STATE_DB.get(bank)
    if not src or not src.exists():
        print(f"找不到 {bank} 的 state.db: {src}", file=sys.stderr)
        return 2
    VEC_DIR.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        db.execute("BEGIN")
        sess, rows, max_all = policy_rows(db)
        db.commit()
    finally:
        db.close()

    sid_idx = {s[0]: i for i, s in enumerate(sess)}
    msgs, msg_ids, msg_sess, keep = [], [], [], []
    for mid, sid, role, content in rows:
        i = sid_idx.get(sid, -1)
        if i < 0:
            continue
        keep.append(mid)
        msgs.append(("问：" if role == "user" else "答：") + (content or "")[:300])
        msg_ids.append(mid)
        msg_sess.append(i)

    ttexts = title_texts(sess)
    started = dt.datetime.now(TZ)
    m = model()

    # 复用既有向量（引导/增量）：按 msg_id 对齐，只嵌入缺失部分
    reused: dict[int, "np.ndarray"] = {}
    if seed_from and Path(seed_from).exists():
        sd = np.load(seed_from, allow_pickle=False)
        seed_ids = [int(x) for x in sd["msg_ids"]]
        seed_vecs = sd["mvecs"]
        want = set(msg_ids)
        for pos, mid in enumerate(seed_ids):
            if mid in want:
                reused[mid] = seed_vecs[pos]
        print(f"[{bank}] 复用向量 {len(reused)}/{len(msg_ids)}，来源 {seed_from}", flush=True)

    missing = [i for i, mid in enumerate(msg_ids) if mid not in reused]
    print(f"[{bank}] 语料 {len(msgs)} 条消息 / {len(ttexts)} 会话，需嵌入 {len(missing)} 条 + {len(ttexts)} 标题…", flush=True)

    tvecs = normalize(np.array(list(m.embed(ttexts, batch_size=64)), dtype="float32")) if ttexts \
        else np.zeros((0, 512), dtype="float32")
    dim = tvecs.shape[1] if len(tvecs) else 512
    mvecs = np.zeros((len(msgs), dim), dtype="float32")
    for i, mid in enumerate(msg_ids):
        if mid in reused:
            mvecs[i] = reused[mid]
    if missing:
        new_vecs = normalize(np.array(list(m.embed([msgs[i] for i in missing], batch_size=64)), dtype="float32"))
        for pos, i in enumerate(missing):
            mvecs[i] = new_vecs[pos]

    titles = [
        {"id": s[0], "title": s[1],
         "date": dt.datetime.fromtimestamp(s[2], TZ).strftime("%Y-%m-%d")}
        for s in sess
    ]
    out, mf = paths(bank)
    np.savez_compressed(
        out, tvecs=tvecs, mvecs=mvecs,
        msg_ids=np.array(msg_ids, dtype="int64"),
        meta=np.array(json.dumps({"titles": titles, "msg_sess": msg_sess, "schema_version": 3}, ensure_ascii=False)),
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "model": MODEL,
        "corpus_policy_version": CORPUS_POLICY_VERSION,
        "bank": bank,
        "built_at": started.isoformat(),
        "mode": "full" if full else "full",
        "session_count": len(sess),
        "indexed_msg_count": len(msgs),
        "max_message_id": int(max_all),
        "corpus_hash": range_hash(rows),
        "elapsed_sec": round((dt.datetime.now(TZ) - started).total_seconds(), 1),
    }
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


def query(bank: str, text: str, k: int = 5) -> int:
    import numpy as np  # noqa: PLC0415

    out, mf = paths(bank)
    if not out.exists():
        print(f"索引不存在: {out}", file=sys.stderr)
        return 2
    d = np.load(out, allow_pickle=False)
    tvecs, mvecs = d["tvecs"], d["mvecs"]
    _meta = json.loads(str(d["meta"]))
    msg_sess = _meta["msg_sess"]
    titles = _meta["titles"]

    qv = normalize(np.array(list(model().embed([INSTRUCT.format(q=text)]))[0]))  # (dim,)
    # 会话分 = 该会话所有消息相似度的最大值（与旧 build2 聚合一致）
    per_sess = {}
    sims = mvecs @ qv
    for idx, s in enumerate(msg_sess):
        s = int(s)
        if s < 0:
            continue
        v = float(sims[idx])
        if v > per_sess.get(s, -1.0):
            per_sess[s] = v
    # 标题通道兜底
    if len(tvecs):
        tsims = tvecs @ qv
        for i, v in enumerate(tsims):
            if float(v) > per_sess.get(i, -1.0):
                per_sess[i] = float(v)
    top = sorted(per_sess.items(), key=lambda kv: kv[1], reverse=True)[:k]
    res = [{"session_id": titles[i]["id"], "title": titles[i]["title"],
            "date": titles[i]["date"], "score": round(float(s), 4)} for i, s in top]
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="星忆会话消息级索引（等价旧 semantic_recall 口径）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="构建/重建索引")
    b.add_argument("--bank", default="default", choices=sorted(STATE_DB))
    b.add_argument("--full", action="store_true", help="全量重建（忽略复用）")
    b.add_argument("--seed-from", default=None, help="复用该 npz 的既有消息向量，只嵌新增")
    q = sub.add_parser("query", help="按模糊描述检索会话")
    q.add_argument("text")
    q.add_argument("--bank", default="default", choices=sorted(STATE_DB))
    q.add_argument("-k", type=int, default=5)
    args = ap.parse_args()
    if args.cmd == "build":
        return build(args.bank, args.full, args.seed_from)
    return query(args.bank, args.text, args.k)


if __name__ == "__main__":
    sys.exit(main())
