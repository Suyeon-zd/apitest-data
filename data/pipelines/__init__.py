# data/pipelines/__init__.py
# Pipeline 子包：每种 target 一个模块，import 即注册
from data.pipelines.base import (               # noqa: F401
    BasePipeline, register, get_pipeline, _PIPELINES,
)

# 触发注册
from data.pipelines import db_pipeline      # noqa: F401, E402
from data.pipelines import api_pipeline     # noqa: F401, E402
from data.pipelines import csv_pipeline     # noqa: F401, E402
from data.pipelines import excel_pipeline   # noqa: F401, E402