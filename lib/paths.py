#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路径与命名空间的单一入口。

所有目录/文件位置在这里集中定义，其余模块只引用，不再各自硬编码。
分发/迁移时改环境变量即可，无需改代码。

环境变量（全部可选，默认值即本机现网布局）：
  MEMD_ROOT         星忆自身目录（代码/数据/向量/tmp）   默认 = 代码目录
  MEMD_BASE         其上层的部署根（memory/state/venv 等）默认 = 代码目录的上级
  MEMD_HERMES_HOME  Hermes home（L0 记忆与 state.db 所在）**必须设置**，无内置缺省
  MEMD_MEMORY_DIR   L1 记忆目录                          默认 <MEMD_BASE>/memory
  MEMD_INDEX_FILE   文件级倒排索引                        默认 <MEMD_BASE>/INDEX.md
  MEMD_STATE_DIR    状态目录（别名/pressure）              默认 <MEMD_BASE>/state
  MEMD_LOG_DIR      memd 日志目录                         默认 <MEMD_BASE>/logs/memd
  MEMD_BANKS        参与索引的 bank 列表（逗号分隔）        默认 default,maojiu,lingyuan,shared
  MEMD_L0_LIMIT     L0 字符上限（未在 config.yaml 指定时）  默认 8000
  FASTEMBED_CACHE   向量模型缓存目录                       默认 <MEMD_BASE>/.cache/fastembed
"""
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT.parent


def _p(env: str, default) -> Path:
    return Path(os.environ.get(env) or default)


# 默认值全部相对代码位置推导：分发后零配置即可跑，本机由环境变量保持现网布局
ROOT = _p("MEMD_ROOT", str(ROOT))
BASE = _p("MEMD_BASE", str(BASE))
HERMES_HOME = _p("MEMD_HERMES_HOME", os.environ.get("HERMES_HOME") or str(BASE / "hermes-home"))

# 星忆自身
BANKS_DIR = ROOT / "data" / "banks"
VEC_DIR = ROOT / "data" / "vectors"
TMP_DIR = ROOT / "tmp"
LOG_DIR = _p("MEMD_LOG_DIR", str(BASE / "logs" / "memd"))

# 记忆与索引
MEMORY_DIR = _p("MEMD_MEMORY_DIR", str(BASE / "memory"))
INDEX_FILE = _p("MEMD_INDEX_FILE", str(BASE / "INDEX.md"))
STATE_DIR = _p("MEMD_STATE_DIR", str(BASE / "state"))
PRESSURE_DIR = STATE_DIR / "memory_pressure"
ALIASES = STATE_DIR / "semantic_aliases.json"

# 向量
EMB_SITE_GLOB = str(BASE / "venvs" / "emb" / "lib" / "python*" / "site-packages")
EMB_PYTHON = BASE / "venvs" / "emb" / "bin" / "python"
FASTEMBED_CACHE = _p("FASTEMBED_CACHE", str(BASE / ".cache" / "fastembed"))

# bank 命名空间
BANKS = tuple(b.strip() for b in (os.environ.get("MEMD_BANKS") or "default,maojiu,lingyuan,shared").split(",") if b.strip())
L0_LIMIT = int(os.environ.get("MEMD_L0_LIMIT") or 8000)


def l0_home(bank: str) -> Path:
    """bank → Hermes profile home。default 为根，其余落在 profiles/<bank>/。"""
    if bank == "default":
        return HERMES_HOME
    return HERMES_HOME / "profiles" / bank


def state_db(bank: str) -> Path:
    return l0_home(bank) / "state.db"


def l0_homes() -> dict:
    """保持旧接口形态：{bank: home}，只含 default 与 profiles 下真实存在的 bank。"""
    out = {"default": HERMES_HOME}
    prof = HERMES_HOME / "profiles"
    if prof.is_dir():
        for d in sorted(prof.iterdir()):
            if d.is_dir():
                out[d.name] = d
    return out
