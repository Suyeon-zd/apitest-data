# data/db/drivers/mysql.py - MySQL 驱动（pymysql）
#
# 跨库：USE 切库（session 级生效），用 _current_db 跟踪避免无谓重复 USE
import itertools
import logging

from data.db.base_driver import BaseDBDriver
from data.db.drivers import register

logger = logging.getLogger(__name__)


@register("mysql")
class MySQLDriver(BaseDBDriver):

    def connect(self) -> bool:
        try:
            import pymysql
        except ImportError:
            logger.warning(
                f"[{self.name}] pymysql 未安装，跳过。安装: pip install pymysql"
            )
            return False
        try:
            # 过滤 type 键与值为 None 的键（database 不填时不传给 pymysql）
            cfg = {k: v for k, v in self.config.items()
                   if k != "type" and v is not None}
            self._conn = pymysql.connect(**cfg)
            self._current_db = cfg.get("database")
            logger.info(
                f"[{self.name}] MySQL 连接成功: "
                f"{cfg.get('host')}:{cfg.get('port')}/"
                f"{cfg.get('database', '<未指定默认库>')}"
            )
            return True
        except Exception as e:
            logger.error(f"[{self.name}] MySQL 连接失败: {e}")
            return False

    def _use_db(self, database):
        """按需 USE 切库，与 _current_db 相同则跳过"""
        if not database or database == self._current_db:
            return
        with self._conn.cursor() as cursor:
            cursor.execute(f"USE `{database}`")
        self._current_db = database

    def query(self, sql, database=None):
        if not self._conn:
            return []
        try:
            self._use_db(database)
            with self._conn.cursor() as cursor:
                cursor.execute(sql)
                columns = [desc[0] for desc in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as e:
            safe_sql = sql[:200] + ("..." if len(sql) > 200 else "")
            logger.error(f"[{self.name}] SQL 执行失败: {safe_sql}\n{e}")
            raise

    def insert_many(self, table, columns, rows, batch_size=1000, database=None):
        if not self._conn:
            return 0
        self._use_db(database)
        col_sql = ", ".join(f"`{c}`" for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        sql = f"INSERT INTO `{table}` ({col_sql}) VALUES ({placeholders})"
        total = 0
        with self._conn.cursor() as cursor:
            iterator = iter(rows)
            while True:
                batch = list(itertools.islice(iterator, batch_size))
                if not batch:
                    break
                cursor.executemany(sql, [tuple(r) for r in batch])
                self._conn.commit()
                total += len(batch)
                logger.debug(f"[{self.name}] 已插入 {total} 行...")
        logger.info(f"[{self.name}] 批量插入 {table} 完成: {total} 行")
        return total