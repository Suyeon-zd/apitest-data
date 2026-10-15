# data/cli.py
# 测试数据批量构造工具入口（精简版：仅 argparse + 分发）
#
# 用法（必须在项目根目录以模块方式运行，即 python -m data.cli，勿直接 python data/cli.py）：
#   python -m data.cli --config data/tables/xxx.yaml
#   python -m data.cli --config data/tables/xxx.yaml --count 500 --dry-run
#   python -m data.cli --config data/tables/xxx.yaml --csv out.csv --count 100000
#   python -m data.cli --config data/tables/resource_ledger_temp.yaml --stage excel,upload
#   python -m data.cli --list-rules
#   python -m data.cli --init my_table
import sys
import logging
import argparse

from data.infra.env_loader import (
    PROJECT_ROOT as _ROOT, load_dotenv, load_env_config, load_table_config,
)
from data.infra.errors import DataError, ConfigError
from data.pipelines import get_pipeline, _PIPELINES  # noqa: F401

logger = logging.getLogger("data.cli")


# ==================== --list-rules 帮助文本 ====================

_RULES_HELP = """
可用的造数规则（在 rules 下配置）：

  fixed          固定值                          {fixed: "在库"}
  choice         从列表随机选（支持 weights）      {choice: [A, B, C], weights: [1, 2, 3]}
  range          [min, max] 区间随机整数          {range: [1, 100]}
  sequence       顺序自增                        {sequence: {start: 1, step: 1}}
  faker          调用任意 Faker 方法              {faker: name, faker_kwargs: {...}}
  id_card        合法 18 位身份证（GB 11643）     {id_card: {area: "341111", min_age: 18}}
  template       占位符模板                      {template: "ZC-{random_int:8}"}
  ref            引用本行其他字段                 {ref: user_name, transform: pinyin}
  switch         按本行另一字段的值映射            {switch: {on: gender, cases: {...}}}
  from_pool      从命名数据池随机抽               {from_pool: pool_dept_ids}
  self_ref       引用本表已生成行的字段            {self_ref: {field: id, root: 0}}
  ip_port        "IPv4:端口"格式                 {ip_port: {port_min: 8000, port_max: 9000}}
  null           固定生成 NULL                   {null: true}
  skip           跳过该字段（交给数据库 DEFAULT） {skip: true}

修饰符（可与任意规则叠加）：
  transform      值转换（可叠加）                transform: [pinyin, upper]
  monotonic      逐行递增（date/datetime/数值）   monotonic: true

template 占位符：
  {random_int}  {random_int:N}  {seq}  {seq:N}  {uuid}  {word}  {name}  {phone}  {email}

transform 转换器：
  pinyin  upper  lower  md5  prefix:xxx  suffix:xxx

表级配置（与 rules 平级）：
  seed: 42                      固定随机种子，同参数产出可复现
  unique: [email, phone]        全表唯一字段（撞车自动重试）
  pools:                        命名数据池（供 from_pool 消费）
    pool_dept: {db: mysql, sql: "SELECT id FROM sys_dept", field: id}
    pool_status: [0, 1, 2]      也支持内联列表
  grouping:                     分组造数（同一组字段跨行一致）
    fields: [asset_code, ips]
    sizes: [1, 2, 3]
    weights: [85, 12, 3]
"""


_INIT_TEMPLATE = """# {name} 表定义：DDL + 数据构造规则
# 运行：python -m data.cli --config data/tables/{name}.yaml --dry-run
# 规则说明：python -m data.cli --list-rules

db: ck                              # 目标库别名（config/<env>.yaml 中 databases 的 key）
count: 100                          # 生成行数
batch_size: 1000                    # 批量插入每批大小
locale: zh_CN                       # Faker 语言

ddl: |-
  CREATE TABLE IF NOT EXISTS {name}
  (
      id            UInt64,
      name          String,
      create_time   DateTime
  )
  ENGINE = MergeTree
  ORDER BY id;

rules:
  id:
    sequence: {{start: 1, step: 1}}
  name:
    faker: name
  create_time:
    faker: date_time_this_year
    monotonic: true
"""


# ==================== 参数解析 ====================

def _parse_args():
    parser = argparse.ArgumentParser(description="批量构造测试数据并写入 MySQL / ClickHouse")
    parser.add_argument("--config", help="表定义 YAML 文件路径（除 --list-rules/--init 外必填）")
    parser.add_argument("--env", default="dev", help="环境：dev/staging/prod，默认 dev")
    parser.add_argument("--db", help="覆盖表定义中的 db（数据库别名）")
    parser.add_argument("--count", type=int, help="覆盖表定义中的 count（生成行数）")
    parser.add_argument("--batch-size", type=int, help="覆盖表定义中的 batch_size")
    parser.add_argument("--dry-run", action="store_true", help="只生成预览，不写库/不调接口")
    parser.add_argument("--csv", help="生成数据导出为 CSV 文件（不连接数据库）")
    parser.add_argument("--seed", type=int, help="覆盖表定义中的 seed（同样参数产出可复现）")
    parser.add_argument("--preview", type=int, default=3, help="预览前 N 行，默认 3")
    parser.add_argument("--preview-format", choices=["auto", "vertical", "table"],
                        default="auto",
                        help="预览布局：auto（列数>8 自动竖版）/ vertical 竖版 / table 横版，默认 auto")
    parser.add_argument("--stage", default="all",
                        help="target=excel 的执行阶段：excel / upload / temp / all，"
                             "支持逗号分隔（如 excel,upload），默认 all")
    parser.add_argument("--rebuild-temp", action="store_true",
                        help="阶段3 先按批次前缀清空 temp 旧数据再插入（重跑时显式启用）")
    parser.add_argument("--validate", action="store_true",
                        help="只校验表定义 YAML（字段名/规则/pool 引用），不生成数据")
    parser.add_argument("--list-rules", action="store_true",
                        help="打印全部造数规则说明后退出")
    parser.add_argument("--init", metavar="NAME",
                        help="在 data/tables/NAME.yaml 生成表定义脚手架后退出")
    return parser.parse_args()


# ==================== --init 脚手架 ====================

def _write_scaffold(name: str):
    target = _ROOT / "data" / "tables" / f"{name}.yaml"
    if target.exists():
        logger.error("[ERROR] 已存在: %s", target)
        sys.exit(1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_INIT_TEMPLATE.format(name=name), encoding="utf-8")
    print(f"[OK] 已生成脚手架: {target}")
    print(f"     编辑后运行: python -m data.cli --config {target.relative_to(_ROOT)} --dry-run")


# ==================== --validate 预检 ====================

def _run_validate(table_config: dict, args):
    """只构造 DataFactory（触发严格校验），不生成任何数据"""
    from data.factory.factory import DataFactory
    from data.factory.ddl_parser import _infer_dialect

    ddl_text = table_config.get("ddl")
    is_api = table_config.get("api") or str(table_config.get("target", "db")).lower() == "api"

    if is_api:
        dialect = "api"
    elif table_config.get("dialect"):
        dialect = str(table_config["dialect"]).lower()
    elif ddl_text:
        dialect = _infer_dialect(ddl_text)
    else:
        dialect = "mysql"

    try:
        DataFactory(
            ddl_text, table_config, dialect=dialect,
            locale=table_config.get("locale", "zh_CN"),
            strict_validate=True,
        )
    except ValueError as e:
        raise ConfigError(str(e)) from e

    print("[OK] 表定义校验通过")


# ==================== 主入口 ====================

def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _resolve_target(table_config: dict, args) -> str:
    """决定本次运行的 Pipeline target"""
    if args.csv:
        return "csv"
    target = str(table_config.get("target", "db")).lower()
    # 兼容：有 api 段但没写 target 时视为 api
    if target == "db" and table_config.get("api"):
        target = "api"
    return target


def main():
    _setup_logging()
    load_dotenv()

    args = _parse_args()

    # 快捷路径：不加载表定义
    if args.list_rules:
        print(_RULES_HELP)
        return
    if args.init:
        _write_scaffold(args.init)
        return
    if not args.config:
        logger.error("[ERROR] 缺少 --config 参数（除非使用 --list-rules / --init）")
        sys.exit(1)

    try:
        table_config = load_table_config(args.config)
        if args.seed is not None:
            table_config["seed"] = args.seed

        # --validate：只校验，不生成
        if args.validate:
            _run_validate(table_config, args)
            return

        # 分发到对应 Pipeline
        target = _resolve_target(table_config, args)
        pipeline_cls = get_pipeline(target)
        if pipeline_cls is None:
            raise ConfigError(f"未知 target: {target}（已注册: {sorted(_PIPELINES)}）")

        env_config = load_env_config(args.env)
        pipeline = pipeline_cls(table_config, env_config, args)
        pipeline.run()

    except DataError as e:
        logger.error("[ERROR] %s", e)
        sys.exit(e.code)
    except FileNotFoundError as e:
        logger.error("[ERROR] %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()