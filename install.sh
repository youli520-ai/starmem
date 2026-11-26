#!/bin/sh
# 星忆（Starmem）安装脚本 · MIT
#
# 用法：
#   ./install.sh                    # 装到当前目录所在位置（默认把本包当作运行目录）
#   PREFIX=/opt/starmem ./install.sh
#   HERMES_PLUGINS=~/.hermes/plugins ./install.sh   # 同时安装 Hermes provider 插件
#
# 只做四件事：建目录 → 初始化库 → （可选）装插件 → 冒烟自检。不做任何删除。
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
CODE=$HERE                    # 代码始终在包目录
DATA=${PREFIX:-$HERE}         # PREFIX 只决定数据落点
PY=${PYTHON:-python3}

echo "== 1/4 环境检查"
command -v "$PY" >/dev/null 2>&1 || { echo "缺少 $PY，请先装 Python 3.10+"; exit 1; }
"$PY" -c 'import sys,sqlite3; assert sys.version_info>=(3,10); c=sqlite3.connect(":memory:"); c.execute("CREATE VIRTUAL TABLE t USING fts5(x)"); print("  python",sys.version.split()[0],"· FTS5 ok")'

echo "== 2/4 初始化目录与库（数据 → $DATA）"
mkdir -p "$DATA/data/banks" "$DATA/data/vectors" "$DATA/tmp"
MEMD_ROOT="$DATA" "$PY" "$CODE/bin/memd" initdb | sed 's/^/  /'

echo "== 3/4 Hermes 插件（可选）"
if [ -n "${HERMES_PLUGINS:-}" ]; then
  mkdir -p "$HERMES_PLUGINS"
  cp -R "$CODE/plugin/starmem-memory" "$HERMES_PLUGINS/" 2>/dev/null || true
  echo "  已复制到 $HERMES_PLUGINS/starmem-memory（需在 config 里启用 provider）"
else
  echo "  跳过（设 HERMES_PLUGINS=<你的 plugins 目录> 可自动安装）"
fi

echo "== 4/4 冒烟自检"
MEMD_ROOT="$DATA" "$PY" "$CODE/bin/starmem-e2e" | sed 's/^/  /'

cat <<EOF

安装完成。
  代码目录   $CODE
  数据目录   $DATA
  启动服务   MEMD_ROOT=$DATA $PY $CODE/bin/memd serve --port 9910
  静态审计   MEMD_ROOT=$DATA $PY $CODE/bin/memd audit
  端到端     MEMD_ROOT=$DATA $PY $CODE/bin/starmem-e2e
  L1 记忆    $DATA/memory/<bank>/*.md（写法见 README「L1 写法」）

不设 PREFIX 时数据落在包目录，代码与数据可分离部署（代码可只读挂载）。
EOF
