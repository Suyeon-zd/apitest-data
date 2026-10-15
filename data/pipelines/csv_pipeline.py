# data/pipelines/csv_pipeline.py
# target: csv —— 流式分批导出 CSV（避免百万行 OOM）
import csv
import logging
from pathlib import Path

from data.db.client import DBManager
from data.infra.env_loader import PROJECT_ROOT
from data.infra.errors import ConfigError, ConnectionError
from data.factory.ddl_parser import _infer_dialect
from data.factory.factory import DataFactory
from data.pipelines.base import BasePipeline, register

logger = logging.getLogger(__name__)


@register("csv")
class CsvPipeline(BasePipeline):

    def run(self):
        tc, args = self.table_config, self.args

        csv_path = Path(args.csv)
        if not csv_path.is_absolute():
            csv_path = (PROJECT_ROOT / csv_path).resolve()
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        ddl_text = tc.get("ddl")
        if not ddl_text:
            raise ConfigError("表定义缺少 ddl 字段")

        count = args.count or tc.get("count", 10)
        batch_size = args.batch_size or tc.get("batch_size", 1000)
        locale = tc.get("locale", "zh_CN")

        # CSV 模式：可完全离线（无 SQL 池）；有 SQL 池时必须连库
        pools_cfg = tc.get("pools") or {}
        has_sql_pool = any(isinstance(c, dict) and c.get("sql") for c in pools_cfg.values())

        dialect = (str(tc["dialect"]).lower() if tc.get("dialect")
                   else _infer_dialect(ddl_text))

        manager = None
        if has_sql_pool:
            manager = DBManager(self.env_config.get("databases") or {})
            if not manager.available:
                raise ConnectionError("无可用数据库连接（SQL 数据池需要）")

        try:
            factory = DataFactory(ddl_text, tc, dialect=dialect,
                                  locale=locale, db_manager=manager)

            print(f"\n目标: CSV 导出（{dialect} 方言，不连库）  "
                  f"表: {factory.table}  生成行数: {count}")

            # 流式分批：内存只驻留一批
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(factory.column_names)
                remaining = count
                first_batch = True
                while remaining > 0:
                    n = min(batch_size, remaining)
                    _, _, rows = factory.build_rows(n)
                    if first_batch:
                        self._preview(factory.column_names,
                                      rows[:args.preview], tc.get("rules"))
                        first_batch = False
                    writer.writerows(rows)
                    remaining -= n
                    logger.info("已导出 %d/%d 行", count - remaining, count)

            print(f"\n[OK] 已导出 {count} 行 CSV: {csv_path}")
        finally:
            if manager:
                manager.close()