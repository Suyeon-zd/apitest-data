# data/db/drivers/transwarp.py - 星环数据库驱动
#
# 兼容产品线：Inceptor / ArgoDB / StellarDB（都注册了别名）
#
# 连接方式（按可用性依次尝试）：
#   1) pyhive.hive —— 兼容 HiveServer2 协议，多数星环产品可用
#   2) quark.dbapi —— 星环官方 quark 驱动（包名以实际交付为准）
#
# 如果你们拿到的驱动包名/接口不同，只需修改 _create_connection 里的
# import 与 connect 参数，其他逻辑（query/insert_many/切库）无需改动。
#
# 跨库：USE 切库（与 MySQL 类似）
import itertools
import logging

from data.db.base_driver import BaseDBDriver
from data.db.drivers import register

logger = logging.getLogger(__name__)


@register("transwarp", "inceptor", "argodb", "stellardb")
class TranswarpDriver(BaseDBDriver):

    def connect(self) -> bool:
        conn = self._create_connection()
        if conn is None:
            return False
        self._conn = conn

        db_name = self.config.get("database")
        if db_name:
            try:
                with self._conn.cursor() as cursor:
                    cursor.execute(f"USE {db_name}")
                self._current_db = db_name
            except Exception as e:
                logger.warning(
                    f"[{self.name}] USE {db_name} 失败（可能库不存在）: {e}"
                )

        logger.info(
            f"[{self.name}] Transwarp 连接成功: "
            f"{self.config.get('host')}:{self.config.get('port', 10000)}/"
            f"{db_name or '<未指定默认库>'}"
        )
        return True

    def _create_connection(self):
        """创建底层连接：按驱动可用性依次尝试

        配置项（从 config/<env>.yaml 的 databases.<name> 读取）：
          host / port / user / password / auth_mechanism / database
          可选：kerberos_service_name / use_ssl / ca_cert
        """
        cfg = self.config
        host = cfg.get("host")
        port = int(cfg.get("port", 10000))
        username = cfg.get("user") or cfg.get("username")
        password = cfg.get("password")
        auth = cfg.get("auth_mechanism", "LDAP")
        database = cfg.get("database")

        # ---------- 方式 1：pyhive.hive（HiveServer2 协议） ----------
        try:
            from pyhive import hive
            return hive.Connection(
                host=host, port=port,
                username=username, password=password,
                auth=auth,
                database=database,
            )
        except ImportError:
            pass
        except Exception as e:
            logger.error(f"[{self.name}] pyhive 连接失败: {e}")
            return None

        # ---------- 方式 2：星环官方 quark 驱动 ----------
        try:
            from quark.dbapi import connect as quark_connect
            return quark_connect(
                host=host, port=port,
                username=username, password=password,
                auth_mechanism=auth,
                database=database,
            )
        except ImportError:
            pass
        except Exception as e:
            logger.error(f"[{self.name}] quark 连接失败: {e}")
            return None

        logger.warning(
            f"[{self.name}] 未找到可用的 Transwarp 驱动。"
            f"请安装 pyhive（pip install pyhive）或星环官方驱动包"
        )
        return None

    def _use_db(self, database):
        if not database or database == self._current_db:
            return
        with self._conn.cursor() as cursor:
            cursor.execute(f"USE {database}")
        self._current_db = database

    def query(self, sql, database=None):
        if not self._conn:
            return []
        try:
            self._use_db(database)
            with self._conn.cursor() as cursor:
                cursor.execute(sql)
                columns = (
                    [desc[0] for desc in cursor.description]
                    if cursor.description else []
                )
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
        sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})"
        total = 0
        with self._conn.cursor() as cursor:
            iterator = iter(rows)
            while True:
                batch = list(itertools.islice(iterator, batch_size))
                if not batch:
                    break
                cursor.executemany(sql, [tuple(r) for r in batch])
                total += len(batch)
                logger.debug(f"[{self.name}] 已插入 {total} 行...")
        logger.info(f"[{self.name}] 批量插入 {table} 完成: {total} 行")
        return total