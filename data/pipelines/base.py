# data/pipelines/base.py
# Pipeline 抽象基类 + target → Pipeline 注册表
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class BasePipeline(ABC):
    """所有 Pipeline 的基类

    子类通过 @register("db") / @register("api") 注册到 _PIPELINES。
    run() 抛 DataError 表示失败，正常返回表示成功。
    """

    def __init__(self, table_config: dict, env_config: dict, args):
        self.table_config = table_config
        self.env_config = env_config
        self.args = args

    @abstractmethod
    def run(self):
        """执行流水线"""

    def _preview(self, columns, rows, rules_config=None):
        """统一预览：前 args.preview 行 + 规则字段分布"""
        from data.infra.preview import preview_data
        preview_rows = rows[:self.args.preview]
        preview_data(columns, preview_rows,
                     rules_config or self.table_config.get("rules"),
                     fmt=self.args.preview_format)


# target → Pipeline 注册表
_PIPELINES = {}


def register(target: str):
    """装饰器：把 Pipeline 子类注册到 target"""
    def deco(cls):
        _PIPELINES[target] = cls
        return cls
    return deco


def get_pipeline(target: str):
    return _PIPELINES.get(target)