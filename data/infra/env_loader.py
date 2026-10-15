# data/infra/env_loader.py
# 环境配置加载：.env + ${VAR} 展开 + config/<env>.yaml 读取
import os
import re
import logging
from pathlib import Path
import yaml
from data.infra.errors import ConfigError

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ${VAR} 与 ${VAR:-default}
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def load_dotenv():
    """加载项目根目录 .env（未安装 python-dotenv 时静默跳过）"""
    dotenv_path = PROJECT_ROOT / ".env"
    if not dotenv_path.exists():
        return
    try:
        from dotenv import load_dotenv as _load
    except ImportError:
        logger.warning("检测到 .env 但未安装 python-dotenv，已跳过（pip install python-dotenv）")
        return
    _load(dotenv_path, override=False, encoding="utf-8")


def expand_env_vars(obj):
    """递归替换 str/dict/list 中的 ${VAR}、${VAR:-默认值}

    注意：core/config_loader.py 中有一份语义完全相同的实现。
    两份**刻意保留**（硬约束：data/ 不得 import core/，需可整体拷贝到其他项目）；
    修改其中一份时请同步另一份，避免插值行为漂移。
    """

    def _replace(match):
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        logger.warning("环境变量 %s 未设置，对应配置项已替换为空串", name)
        return ""

    if isinstance(obj, str):
        return _ENV_VAR_PATTERN.sub(_replace, obj)
    if isinstance(obj, dict):
        return {k: expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env_vars(v) for v in obj]
    return obj


def load_env_config(env: str) -> dict:
    """读取 config/<env>.yaml 并展开 ${VAR}"""
    env_path = PROJECT_ROOT / "config" / f"{env}.yaml"
    if not env_path.exists():
        raise ConfigError(f"环境配置文件不存在: {env_path}")
    with open(env_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return expand_env_vars(raw)


def load_table_config(config_path: str) -> dict:
    """读取表定义 YAML（相对路径基于项目根）"""
    p = Path(config_path)
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    if not p.is_file():
        raise ConfigError(f"表定义文件不存在: {p}")
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}