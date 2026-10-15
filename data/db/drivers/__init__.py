# data/db/drivers/__init__.py - 数据库驱动注册表
#
# 新驱动接入方式：
#   1) 在 drivers/ 下新建 xxx.py
#   2) 用 @register("type1", "type2") 装饰驱动类
#   3) 在文件末尾 import 触发注册（见本文件底部）
#   无需改动 client.py


_DRIVERS = {}


def register(*types):
    """装饰器：把驱动类注册到 type 别名（支持多个别名，大小写不敏感）"""
    def deco(cls):
        for t in types:
            _DRIVERS[t.lower()] = cls
        return cls
    return deco


def get_driver_class(db_type: str):
    """按 type 取驱动类，未注册返回 None"""
    return _DRIVERS.get((db_type or "").lower())


def available_types() -> list:
    """返回已注册的全部 type 别名（排序）"""
    return sorted(_DRIVERS)


# ---------- 触发注册（import 即注册） ----------
from data.db.drivers import mysql       # noqa: F401, E402
from data.db.drivers import clickhouse  # noqa: F401, E402
from data.db.drivers import transwarp   # noqa: F401, E402