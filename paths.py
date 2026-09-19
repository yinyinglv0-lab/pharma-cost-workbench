# -*- coding: utf-8 -*-
"""统一配置模块（F27 修复）：模型/数据路径收敛到一处，支持环境变量覆盖。

用法：
  from paths import DATA_DIR, MODEL_PATH, RERANKER_PATH, DASHSCOPE_API_KEY

环境变量覆盖：
  BGE_M3_PATH         向量模型目录
  BGE_RERANKER_PATH   reranker 模型目录
  DASHSCOPE_API_KEY   DashScope API Key（不写死在代码中）
"""
import os
from pathlib import Path

# 所有数据/脚本锚定到本文件所在目录（修复"相对启动目录"问题）
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('COST_DATA_DIR', str(BASE_DIR))).resolve()
MANAGED_DIR = Path(os.environ.get('COST_MANAGED_DIR', str(DATA_DIR / 'managed'))).resolve()

# Portable defaults; existing workstation paths remain a detected local convenience.
_MODEL_HOME = BASE_DIR / 'models'
_LOCAL_MODEL_HOME = Path('D:/MyModels')
MODEL_PATH = os.environ.get('BGE_M3_PATH', str(_LOCAL_MODEL_HOME / 'bge-m3-local' if (_LOCAL_MODEL_HOME / 'bge-m3-local').is_dir() else _MODEL_HOME / 'bge-m3'))
RERANKER_PATH = os.environ.get('BGE_RERANKER_PATH', str(_LOCAL_MODEL_HOME / 'bge-reranker-v2-m3' if (_LOCAL_MODEL_HOME / 'bge-reranker-v2-m3').is_dir() else _MODEL_HOME / 'bge-reranker-v2-m3'))
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")

CHROMA_PATH = os.environ.get('COST_CHROMA_PATH', str(DATA_DIR / 'kb' / 'chroma_db'))
GRAPH_FILE = str(DATA_DIR / 'knowledge_graph.json')
SOURCES_REGISTER = str(DATA_DIR / 'sources_register.json')
KB_STATE_FILE = str(DATA_DIR / 'kb_update_state.json')   # kb_update 源文件哈希注册表

USE_FP16 = False  # CPU 统一用 False（修复 kb_update 与其他脚本不一致问题）
