# data/pipelines/excel_pipeline.py
# target: excel —— 三阶段流水线：生成 Excel / 上传导入 / 配对插 temp
# --stage 支持逗号分隔（如 --stage excel,upload 只跑前两阶段）
import logging
from pathlib import Path

from data.db.client import DBManager
from data.infra.env_loader import PROJECT_ROOT
from data.infra.errors import ConfigError, ConnectionError, PartialFailure
from data.factory.factory import DataFactory
from data.pipelines.base import BasePipeline, register

logger = logging.getLogger(__name__)

_VALID_STAGES = ("excel", "upload", "temp")


@register("excel")
class ExcelPipeline(BasePipeline):

    def run(self):
        tc, args = self.table_config, self.args

        tpl_cfg = tc.get("template") or {}
        tpl_path_raw = tpl_cfg.get("path")
        if not tpl_path_raw:
            raise ConfigError("表定义缺少 template.path（官方导入母版路径）")
        tpl_path = (Path(tpl_path_raw) if Path(tpl_path_raw).is_absolute()
                    else (PROJECT_ROOT / tpl_path_raw).resolve())

        count = args.count or tc.get("count", 1_000_000)
        rows_per_file = tc.get("rows_per_file", 10000)
        insert_batch_size = tc.get("insert_batch_size", 50000)
        batch_prefix = tc.get("batch_prefix", "BATCH-")
        grouping = tc.get("grouping") or {}
        output_dir = (PROJECT_ROOT / tc.get("output_dir", "output/asset_excel")).resolve()

        stages = self._parse_stages(args.stage)

        # 母版选项 sheet → 规则引擎数据池
        excel_pools = tc.get("excel_pools") or {}
        if excel_pools:
            from data.seeders.excel.writer import read_template_options
            sheet_values = read_template_options(tpl_path, list(excel_pools.values()))
            tc["pools"] = {key: sheet_values[sheet] for key, sheet in excel_pools.items()}

        # SQL 池与模板下拉取交集（仅 excel 阶段需要，dry-run 时跳过）
        sql_pool_cfg = tc.get("unit_sql_pool")
        if sql_pool_cfg and not args.dry_run and "excel" in stages:
            self._intersect_unit_pool(tc, sql_pool_cfg)

        manager = None
        ck_client = None
        try:
            if "excel" in stages:
                self._stage_excel(tc, tpl_path, tpl_cfg, output_dir,
                                  count, rows_per_file, grouping)
                if args.dry_run:
                    return

            if "upload" in stages or "temp" in stages:
                manager = DBManager(self.env_config.get("databases") or {})
                try:
                    ck_client = manager.get("ck")
                except ValueError as e:
                    raise ConnectionError(str(e)) from e

            if "upload" in stages:
                self._stage_upload(tc, output_dir, batch_prefix, ck_client)

            if "temp" in stages:
                self._stage_temp(tc, ck_client, batch_prefix, count, insert_batch_size)
        finally:
            if manager:
                manager.close()

    # ---------- 阶段拆分 ----------

    @staticmethod
    def _parse_stages(stage_arg: str):
        if stage_arg == "all":
            return list(_VALID_STAGES)
        stages = [s.strip() for s in stage_arg.split(",") if s.strip()]
        if not stages:
            raise ConfigError("--stage 为空，可选 excel/upload/temp/all")
        for s in stages:
            if s not in _VALID_STAGES:
                raise ConfigError(f"未知阶段: {s}（可选: excel/upload/temp，逗号分隔）")
        return stages

    def _intersect_unit_pool(self, tc, sql_pool_cfg):
        manager = DBManager(self.env_config.get("databases") or {})
        try:
            client = manager.get(sql_pool_cfg.get("db", "mysql"))
            results = client.query(sql_pool_cfg["sql"])
            sql_names = {r[sql_pool_cfg["field"]] for r in results}
            for key in (sql_pool_cfg.get("intersect") or []):
                if key not in tc.get("pools", {}):
                    continue
                intersected = [v for v in tc["pools"][key] if v in sql_names]
                if not intersected:
                    raise ConfigError(
                        f"模板选项池 '{key}' 与 sys_dept 无交集，请检查 sys_dept 或模板下拉"
                    )
                tc["pools"][key] = intersected
                logger.info("数据池 '%s' 与 sys_dept 取交集: %d 个值", key, len(intersected))
        finally:
            manager.close()

    def _stage_excel(self, tc, tpl_path, tpl_cfg, output_dir,
                     count, rows_per_file, grouping):
        factory = DataFactory(None, tc)
        if self.args.dry_run:
            preview_n = min(count, max(self.args.preview, 1))
            columns, groups = factory.build_grouped_rows(preview_n, grouping)
            preview_rows = [row for group in groups for row in group][:self.args.preview]
            print(f"\n目标: Excel 母版 {tpl_path.name} / sheet {tpl_cfg.get('sheet')}"
                  f"  配置总行数: {count}（每文件 {rows_per_file} 行，数据从第 3 行起）")
            self._preview(columns, preview_rows, tc.get("rules"))
            print("\n[dry-run] 仅预览，未生成文件。")
            return

        from data.seeders.excel.writer import ExcelBatchWriter
        writer = ExcelBatchWriter(
            template_path=tpl_path,
            sheet_name=tpl_cfg.get("sheet", "应用系统"),
            output_dir=output_dir,
            file_prefix="asset_app_",
            data_start_row=int(tpl_cfg.get("data_start_row", 3)),
        )
        writer.write_all(factory, count, rows_per_file, grouping)
        print(f"\n[OK] Excel 全部生成完成，输出目录: {output_dir}")

    def _stage_upload(self, tc, output_dir, batch_prefix, ck_client):
        from data.seeders.excel.importer import AssetImporter
        imp_cfg = tc.get("import_api") or {}
        if not imp_cfg.get("path"):
            raise ConfigError("表定义缺少 import_api.path")
        importer = AssetImporter(
            base_url=self.env_config.get("base_url", ""),
            env_config=self.env_config,
            excel_dir=output_dir,
            import_path=imp_cfg["path"],
            import_form=imp_cfg.get("form") or {},
            batch_prefix=batch_prefix,
            ck_client=ck_client,
            timeout=imp_cfg.get("timeout") or (5, 120),
        )
        summary = importer.run()
        if summary["failed"]:
            raise PartialFailure(f"导入失败 {summary['failed']} 个文件", code=2)
        if summary["warned"]:
            raise PartialFailure(f"总对账行数不符", code=2)

    def _stage_temp(self, tc, ck_client, batch_prefix, count, insert_batch_size):
        from data.seeders.excel.temp_seeder import TempSeeder
        st = tc.get("stage_temp") or {}
        if not st.get("ddl"):
            raise ConfigError("表定义缺少 stage_temp.ddl")
        seeder = TempSeeder(
            ck_client=ck_client,
            temp_ddl=st["ddl"],
            pair_keys=st.get("pair_keys") or [],
            diff_rules=st.get("diff_rules") or {},
            batch_prefix=batch_prefix,
            expected_count=count,
            insert_batch_size=insert_batch_size,
            min_ratio=tc.get("min_ledger_ratio", 0.95),
            rebuild=self.args.rebuild_temp,
        )
        seeder.run()