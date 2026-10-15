# data/pipelines/db_pipeline.py
# target: db（默认）—— 流式生成数据并分批插入 MySQL / CK
import itertools
import logging

from data.db.client import DBManager
from data.infra.errors import ConfigError, ConnectionError
from data.factory.ddl_parser import _infer_dialect
from data.factory.factory import DataFactory
from data.pipelines.base import BasePipeline, register

logger = logging.getLogger(__name__)


@register("db")
class DbPipeline(BasePipeline):

    def run(self):
        tc, args = self.table_config, self.args

        db_name = args.db or tc.get("db")
        if not db_name:
            raise ConfigError("未指定目标库：请配置 db 字段或用 --db 传入别名（如 mysql / ck）")

        ddl_text = tc.get("ddl")
        if not ddl_text:
            raise ConfigError("表定义缺少 ddl 字段")

        count = args.count or tc.get("count", 10)
        batch_size = args.batch_size or tc.get("batch_size", 1000)
        locale = tc.get("locale", "zh_CN")
        database = tc.get("database")

        databases_cfg = self.env_config.get("databases") or {}
        target_cfg = databases_cfg.get(db_name) or {}
        if tc.get("dialect"):
            dialect = str(tc["dialect"]).lower()
        elif target_cfg.get("type") in ("clickhouse", "ck"):
            dialect = "clickhouse"
        elif target_cfg:
            dialect = "mysql"
        else:
            dialect = _infer_dialect(ddl_text)

        pools_cfg = tc.get("pools") or {}
        has_sql_pool = any(isinstance(c, dict) and c.get("sql") for c in pools_cfg.values())

        manager = None
        if has_sql_pool or not args.dry_run:
            manager = DBManager(databases_cfg)
            if not manager.available:
                raise ConnectionError("无可用数据库连接，请检查 config 中的 databases 配置")

        try:
            # 先取连接（惰性 DBManager 在此才真正连库）：连不上即时失败，不白跑一遍数据生成
            client = None
            if not args.dry_run:
                try:
                    client = manager.get(db_name)
                except ValueError as e:
                    raise ConnectionError(str(e)) from e

            factory = DataFactory(ddl_text, tc, dialect=dialect,
                                  locale=locale, db_manager=manager)
            table, columns = factory.table, factory.column_names

            print(f"\n目标: {db_name}（{dialect}）  表: {table}  生成行数: {count}")

            if args.dry_run:
                # dry-run：只生成预览所需的少量行，无需全量生成
                preview_rows = list(itertools.islice(factory.iter_rows(count), args.preview))
                self._preview(columns, preview_rows, tc.get("rules"))
                print("\n[dry-run] 仅预览，未写入数据库。")
                return

            # 流式生成 + 分批插入：生成器直接交给 insert_many，
            # 由驱动按 batch_size 拉取，内存只驻留一批（百万行也不会 OOM）
            row_iter = factory.iter_rows(count)
            preview_rows = list(itertools.islice(row_iter, args.preview))
            self._preview(columns, preview_rows, tc.get("rules"))

            inserted = client.insert_many(
                table, columns, itertools.chain(preview_rows, row_iter),
                batch_size=batch_size, database=database,
            )
            target = f"{database}.{table}" if database else table
            print(f"\n[OK] 成功写入 {inserted} 行到 {db_name}.{target}")
        finally:
            if manager:
                manager.close()