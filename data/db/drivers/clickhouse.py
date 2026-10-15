# data/db/drivers/clickhouse.py - ClickHouse 驱动（clickhouse-connect）
#
# 跨库：
#   - insert_many 的 database 参数构造 `database.table` 限定名（防重复前缀）
#   - query 不切库，跨库请在 SQL 中写 `db.table` 限定名
import itertools
import logging

from data.db.base_driver import BaseDBDriver
from data.db.drivers import register

logger = logging.getLogger(__name__)


@register("clickhouse", "ck")
class ClickHouseDriver(BaseDBDriver):

    def connect(self) -> bool:
        try:
            import clickhouse_connect
        except ImportError:
            logger.warning(
                f"[{self.name}] clickhouse-connect 未安装，跳过。"
                f"安装: pip install clickhouse-connect"
            )
            return False
        try:
            db_name = self.config.get("database") or "default"
            self._conn = clickhouse_connect.get_client(
                host=self.config.get("host"),
                port=self.config.get("port", 8123),
                username=self.config.get("username") or self.config.get("user", "default"),
                password=self.config.get("password", ""),
                database=db_name,
            )
            self._current_db = db_name
            logger.info(
                f"[{self.name}] ClickHouse 连接成功: "
                f"{self.config.get('host')}:{self.config.get('port', 8123)}/{db_name}"
            )
            return True
        except Exception as e:
            logger.error(f"[{self.name}] ClickHouse 连接失败: {e}")
            return False

    def query(self, sql, database=None):
        if not self._conn:
            return []
        try:
            # 跨库查询请用 db.table 限定名
            result = self._conn.query(sql)
            columns = result.column_names
            return [dict(zip(columns, row)) for row in result.result_rows]
        except Exception as e:
            safe_sql = sql[:200] + ("..." if len(sql) > 200 else "")
            logger.error(f"[{self.name}] SQL 执行失败: {safe_sql}\n{e}")
            raise

    def insert_many(self, table, columns, rows, batch_size=1000, database=None):
        if not self._conn:
            return 0
        # CK 表名限定（防重复：table 已含 "." 时不再加 database 前缀）
        if database and "." not in table:
            target_table = f"{database}.{table}"
        else:
            target_table = table
        total = 0
        iterator = iter(rows)
        while True:
            batch = list(itertools.islice(iterator, batch_size))
            if not batch:
                break
            self._conn.insert(target_table, [list(r) for r in batch], column_names=columns)
            total += len(batch)
            logger.debug(f"[{self.name}] 已插入 {total} 行...")
        logger.info(f"[{self.name}] 批量插入 {target_table} 完成: {total} 行")
        return total