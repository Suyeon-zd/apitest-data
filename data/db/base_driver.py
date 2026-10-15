# data/db/base_driver.py - 数据库驱动抽象基类
#
# 所有具体驱动（mysql/clickhouse/transwarp/...）必须继承 BaseDBDriver，
# 并实现 connect/query/insert_many 三个方法。
from abc import ABC, abstractmethod


class BaseDBDriver(ABC):
    """数据库驱动基类

    子类职责：
      - connect()：建立连接，成功 True，失败 False（失败原因由子类打日志）
      - query()：执行查询，返回 [{'col': val, ...}, ...]；失败抛异常
      - insert_many()：批量插入，rows 为可迭代对象；返回成功插入行数

    共性字段：
      - self.name：别名（对应 config.databases 的 key）
      - self.config：该库的完整配置 dict
      - self._conn：底层连接对象（子类自己维护）
      - self._current_db：当前默认库名（用于 USE 切库去重）
    """

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self._conn = None
        self._current_db = None

    @abstractmethod
    def connect(self) -> bool:
        """建立连接。成功 True，失败 False（子类自己打日志说明原因）"""
        raise NotImplementedError

    @abstractmethod
    def query(self, sql: str, database=None) -> list:
        """执行查询 SQL，返回 [{'col': val, ...}, ...]"""
        raise NotImplementedError

    @abstractmethod
    def insert_many(self, table, columns, rows, batch_size=1000, database=None) -> int:
        """批量插入，返回成功插入行数"""
        raise NotImplementedError

    def close(self):
        """默认关闭实现：子类可覆写以处理特殊断开逻辑"""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None