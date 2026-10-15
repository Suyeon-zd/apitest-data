# data/pipelines/api_pipeline.py
# target: api —— 逐行调用业务接口批量造数
import logging

from data.infra.errors import ConfigError, PartialFailure
from data.factory.factory import DataFactory
from data.seeders.http import HttpSeeder
from data.pipelines.base import BasePipeline, register

logger = logging.getLogger(__name__)


@register("api")
class ApiPipeline(BasePipeline):

    def run(self):
        tc, args = self.table_config, self.args

        api_spec = tc.get("api") or {}
        if not api_spec.get("path"):
            raise ConfigError("target=api 时必须在表定义中配置 api.path")

        count = args.count or tc.get("count", 10)
        locale = tc.get("locale", "zh_CN")

        factory = DataFactory(None, tc, dialect="api", locale=locale)
        table, columns, rows = factory.build_rows(count)

        print(f"\n目标: 接口 {api_spec.get('method', 'POST')} {api_spec['path']}"
              f"（调接口，不插库）  生成行数: {count}")
        self._preview(columns, rows, tc.get("rules"))

        if args.dry_run:
            print("\n[dry-run] 仅预览，未调用接口。")
            return

        seeder = HttpSeeder(
            base_url=self.env_config.get("base_url", ""),
            env_config=self.env_config,
            api_spec=api_spec,
            timeout=self.env_config.get("timeout") or (5, 10),
            stop_on_error=bool(tc.get("stop_on_error", False)),
        )
        _, failures = seeder.seed(columns, rows)
        if failures:
            raise PartialFailure(f"接口造数存在 {len(failures)} 条失败", code=2)