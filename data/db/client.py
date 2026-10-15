# data/db/client.py - 数据库客户端（对外统一接口，内部按 type 分发到具体 driver）
#
# 独立性约定：本文件只用标准库 logging，不 import core.*，
# 使 data/ 目录可整体拷贝到其他项目独立使用（日志配置由调用方负责）。
#
# 设计：新增数据库只需在 data/db/drivers/ 下加一个驱动文件，
#      在 client.py 无需任何改动。
import logging

from data.db.drivers import get_driver_class, available_types

logger = logging.getLogger(__name__)


class DBClient:
    """单库客户端：按 db_config['type'] 分发到对应 driver

    支持的 type（由 drivers/ 下注册决定）：
      - mysql                → MySQLDriver
      - clickhouse / ck      → ClickHouseDriver
      - transwarp / inceptor / argodb / stellardb → TranswarpDriver

    跨库支持由各 driver 自行处理（MySQL/星环走 USE 切库，CK 走 db.table 限定名）。
    """

    def __init__(self, name, db_config):
        self.name = name
        self.db_type = db_config.get("type", "mysql").lower()
        driver_cls = get_driver_class(self.db_type)
        if driver_cls is None:
            raise ValueError(
                f"不支持的数据库类型: {self.db_type}，可用: {available_types()}"
            )
        self._driver = driver_cls(name, db_config)

    def connect(self):
        """建立连接，成功返回 self，失败返回 None（便于链式调用与判空）"""
        if self._driver.connect():
            return self
        return None

    def query(self, sql, database=None):
        """执行查询 SQL，返回 [{'col': val, ...}, ...]"""
        return self._driver.query(sql, database)

    def insert_many(self, table, columns, rows, batch_size=1000, database=None):
        """批量插入，rows 为可迭代对象（list/generator 均可）；返回成功插入行数"""
        return self._driver.insert_many(table, columns, rows, batch_size, database)

    def close(self):
        self._driver.close()


class DBManager:
    """多数据库管理器：按别名管理多个 DBClient。

    在 config 中通过 databases 字典配置，key 为别名（如 mysql / ck / transwarp）。
    YAML 的 db_compare 断言与表配置通过 db 字段指定别名选用对应库。

    惰性连接：构造时不连库，只有首次 get(别名) 时才真正建立连接，
    与 conftest 的惰性策略保持一致；未用到的库不会被连接，也不受其可用性影响。
    """

    def __init__(self, databases_config):
        self._configs = dict(databases_config or {})
        self._clients = {}

    def get(self, name):
        """按别名取库（首次访问时建立连接）；未配置或连接失败时抛 ValueError"""
        client = self._clients.get(name)
        if client:
            return client

        cfg = self._configs.get(name)
        if not cfg:
            raise ValueError(
                f"数据库 '{name}' 未配置，已配置别名: {list(self._configs) or '无'}"
            )
        try:
            client = DBClient(name, cfg).connect()
        except ValueError as e:            # 不支持的 type 等初始化错误
            raise ValueError(f"数据库 '{name}' 初始化失败: {e}") from e
        if not client:
            raise ValueError(
                f"数据库 '{name}' 连接失败，请检查 config/<env>.yaml 的 databases 配置与网络连通性"
            )
        self._clients[name] = client
        logger.info(f"数据库管理器已就绪，已连接库: {list(self._clients.keys())}")
        return client

    @property
    def available(self):
        """已配置的库别名列表（惰性：不代表已建立连接）"""
        return list(self._configs.keys())

    def close(self):
        for client in self._clients.values():
            client.close()
        self._clients.clear()