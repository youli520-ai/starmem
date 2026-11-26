# 星忆 Starmem

外挂记忆库：**文件是真源，库只做索引**。给 Hermes 之类的 agent 提供跨会话的长期记忆——写入靠人/agent 写 Markdown，检索走 守护进程 `memd`，召回命中后回源读文件。

- 单文件服务：`bin/memd`（约 750 行 Python，**只用标准库**：http.server + sqlite3）
- 零依赖可跑：没有 jieba 退 bigram、没有 fastembed 退纯 BM25
- 数据自持：SQLite + FTS5 + Markdown，不绑任何云服务

## 架构（一句话版）

```
人类/agent 写 Markdown ──► memd 索引（标题 + 路径 + hash + 分词/向量）──► 检索
        ▲                                      │
        └────────── 命中后回源读正文 ◄──────────┘
```

正文只存在文件里；库里有 `facts.text = NULL`，但 **FTS 会存标题+正文的分词全文**（可反查关键词、不能还原原文）。这一点决定了：**库文件也是敏感数据，别把 data/ 传出去**。

## 安装

```bash
tar xzf starmem-full-*.tar.gz && cd starmem-full-*
./install.sh                       # 建目录 + initdb + 六步自检
python3 bin/memd serve &           # 默认 127.0.0.1:9910
curl -s localhost:9910/v1/health
```

带向量模型（full 包自带 `models/`）：

```bash
export FASTEMBED_CACHE=$PWD/models
pip install fastembed            # 或复用已有 venv，见 MEMD_EMB_PYTHON
```

**从源码仓库安装时模型怎么来**（本仓库不含模型权重）：

| 方式 | 做法 |
|---|---|
| 自动下载 | `pip install fastembed`，首次运行拉 `BAAI/bge-small-zh-v1.5`（约 90MB） |
| 离线包 | 从 [Releases](../../releases) 下 `starmem-full-*.tar.gz`，解压后 `export FASTEMBED_CACHE=<解压目录>/models` |
| 不装 | 无模型自动降级 **纯 BM25**，功能不缺、只少语义召回 |

## 配置（全部可选，改环境变量即可）

| 变量 | 默认 | 作用 |
|---|---|---|
| `MEMD_ROOT` | 代码目录 | 数据落点（banks/vectors/tmp） |
| `MEMD_BASE` | `MEMD_ROOT/..` | 上层部署根（memory/state/venvs） |
| `MEMD_MEMORY_DIR` | `<BASE>/memory` | L1 记忆目录 |
| `MEMD_INDEX_FILE` | `<BASE>/INDEX.md` | 文件级倒排索引 |
| `MEMD_STATE_DIR` | `<BASE>/state` | 别名、容量 pressure |
| `MEMD_HERMES_HOME` | `$HERMES_HOME`（未设时需显式配置） | L0 记忆与 state.db 位置 |
| `MEMD_BANKS` | `default,maojiu,lingyuan,shared` | 参与的 bank（逗号分隔） |
| `MEMD_PORT` / `MEMD_BIND` | `9910` / `127.0.0.1` | 监听 |
| `MEMD_TOKEN` | 空 | 设了就要求 Bearer（loopback 外暴露时务必设） |
| `FASTEMBED_CACHE` | `<BASE>/.cache/fastembed` | 向量模型缓存 |
| `MEMD_EMB_PYTHON` | `<BASE>/venvs/emb/bin/python` | 跑向量/会话检索的解释器 |
| `MEMD_SESSION_RECALL_PY` | `<BASE>/scripts/session_recall.py` | 会话检索脚本（可选组件） |

## L1 写法（最重要的部分）

文件放在 `memory/<bank>/<domain>.md`，**domain = 文件名**。索引器按 `## ` 切条目：

```markdown
# 文件级标题（不索引）

前言也不索引（含 front-matter，整块丢弃、无告警）

## 备份策略
rsync 到 /backup，每日 03:30，失败重试一次。

## 端口分配
9910 元数据面；3000 浏览器面。
```

硬规则（**由代码决定，不是约定**）：

| 规则 | 后果 |
|---|---|
| 每条必须有 `## 标题` | 没有 `##` 的文件**整份不入库**（`memd audit` 会报 no_entries） |
| 标题就是身份 | 改标题 = 删旧+建新，`learned_at` 重置、历史断链；改正文 = 同条更新，时间不变 |
| `###` 不切分 | 小标题并入所属条目 |
| front-matter 不解析 | 写 `type/tier` 等于没写；**不要依赖它** |
| 空正文允许 | 只入标题（不推荐） |

自检与修复：

```bash
python3 bin/memd audit            # 退出码 1 = 有硬错误
python3 bin/memd audit --json     # 机器可读
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/health` | 健康 + 各 bank 计数（无需鉴权） |
| POST | `/v1/recall` | 检索：`{"query":"...","bank":"default","budget":400}` |
| GET | `/v1/session_recall?q=&bank=&k=` | 会话消息级检索（**GET + query**，不是 POST） |
| GET | `/v1/stats` | 各 bank 统计 |
| POST | `/v1/turn` | 投递一轮对话（进抽取队列） |
| POST | `/v1/retain` | 触发 **L0** 重索引（**不会**刷 L1） |
| POST | `/v1/forget` | 软删/硬删；⚠️ 之后任何一次 index 都不会让它复活（v0.1.1 起） |
| POST | `/v1/consolidate` | 规则提案（不调 LLM） |

CLI：

```bash
bin/memd index --all        # 重建索引（顺带重建 INDEX.md）
bin/memd recall "查询" --bank default --budget 400
bin/memd snapshot           # 一致性快照
bin/memd audit              # 静态度量
bin/memd audit --e2e        # 端到端六步（临时库，不碰生产）
```

## 与 Hermes 集成

`plugin/starmem-memory` 是 Hermes 的 MemoryProvider：`prefetch` 注入召回结果、`on_memory_write` 通知 L0 变更、`sync_turn` 投递对话。

```yaml
# config.yaml
memory:
  provider: starmem-memory
```

插件通过 `STAREM_ENDPOINT`（默认 `http://127.0.0.1:9910`）连服务。

## 运维

```bash
memd audit --e2e                  # 六步：建条目→改正文→删条目→软删→FTS→归档
memd snapshot                     # 一致性快照（sqlite backup API）
sqlite3 data/banks/default.db 'PRAGMA integrity_check'
```

**备份建议**：白天快照库 + 定期打包 `memory/` 目录。索引可重建，正文不可重建——优先保文件。

## 已知边界

- **会话检索需要额外脚本**：`/v1/session_recall` 依赖 `session_recall.py`（读 Hermes `state.db` 的 schema），本包不含；未配置时该接口直接报错，其余功能不受影响。
- **L0（MEMORY.md/USER.md）是 Hermes 的东西**：本服务只索引，不接管注入。
- **单写者**：多个 memd 同时写同一 bank 会争 SQLite 写锁，务必只跑一个实例。
- **`memory/` 下非 bank 目录**（如 `sessions/`、`benchmarks/`）会被 INDEX 收录，但不会被 L1 索引。

## 许可

MIT，见 `LICENSE`。
