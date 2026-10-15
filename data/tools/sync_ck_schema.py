# data/tools/sync_ck_schema.py
# 从源 ClickHouse 同步所有库表结构到目标 ClickHouse（仅 DDL，不同步数据）
#
# 用法（项目根目录执行）：
#   python -m data.tools.sync_ck_schema                # 同步全部库表
#   python -m data.tools.sync_ck_schema --dry-run      # 只打印 DDL，不执行
#   python -m data.tools.sync_ck_schema --only ODS     # 只同步 ODS 库
#   python -m data.tools.sync_ck_schema --skip-errors  # 单表失败跳过继续
#
# 依赖：pip install clickhouse-connect
import argparse
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ==================== 连接配置（按需修改） ====================
SOURCE_CK = {
    "host": "10.148.242.206",
    "port": 8123,
    "username": "issa",
    "password": os.environ.get("CK_PASSWORD", ""),   # 从 .env / 环境变量注入，不明文入库
}

TARGET_CK = {
    "host": "10.148.242.214",
    "port": 8123,
    "username": "default",
    "password": os.environ.get("CK_PASSWORD", ""),   # 从 .env / 环境变量注入，不明文入库
}

# ClickHouse 系统库，默认跳过（不同步这些库的结构）
SYSTEM_DBs = {
    "INFORMATION_SCHEMA", "information_schema",
    "_temporary_and_external_tables", "system", "default",
    "FILES", "files",
}


def get_client(cfg: dict):
    """连接 ClickHouse，返回 clickhouse_connect 客户端"""
    import clickhouse_connect
    client = clickhouse_connect.get_client(
        host=cfg["host"],
        port=cfg.get("port", 8123),
        username=cfg.get("username", "default"),
        password=cfg.get("password", ""),
    )
    # 验证连接
    result = client.query("SELECT 1")
    assert result.result_rows[0][0] == 1
    return client


def list_databases(client) -> list:
    """查询所有库（排除系统库）"""
    rows = client.query("SELECT name FROM system.databases ORDER BY name")
    return [r[0] for r in rows.result_rows if r[0] not in SYSTEM_DBs]


def list_tables(client, db: str) -> list:
    """查询某库下所有表（排除视图，只要实表）"""
    sql = (
        "SELECT name, engine FROM system.tables "
        "WHERE database = {db:Str} "
        "AND engine NOT IN ('View', 'MaterializedView', 'LiveView') "
        "ORDER BY name"
    )
    rows = client.query(sql, parameters={"db": db})
    return [(r[0], r[1]) for r in rows.result_rows]


def get_create_ddl(client, db: str, table: str) -> str:
    """获取单张表的 CREATE TABLE 语句"""
    rows = client.query(f"SHOW CREATE TABLE `{db}`.`{table}`")
    return rows.result_rows[0][0]


def execute_on_target(target_client, sql: str, db: str, table: str):
    """在目标 CK 执行 DDL，失败抛异常"""
    try:
        target_client.command(sql)
        logger.info("  [OK] %s.%s", db, table)
    except Exception as e:
        logger.error("  [FAIL] %s.%s: %s", db, table, e)
        raise


def sync(args):
    """主流程：源 CK 查 DDL → 目标 CK 建库建表"""
    logger.info("连接源 CK: %s", SOURCE_CK["host"])
    source = get_client(SOURCE_CK)

    logger.info("连接目标 CK: %s", TARGET_CK["host"])
    target = get_client(TARGET_CK)

    # 获取源库列表
    databases = list_databases(source)
    if args.only:
        databases = [db for db in databases if db == args.only]
    if not databases:
        logger.warning("没有需要同步的库")
        return

    logger.info("待同步库: %s", databases)

    total_tables = 0
    total_ok = 0
    failures = []

    for db in databases:
        # 建库（IF NOT EXISTS，已存在则跳过）
        create_db_sql = f"CREATE DATABASE IF NOT EXISTS `{db}`"
        if args.dry_run:
            logger.info("[dry-run] %s", create_db_sql)
        else:
            try:
                target.command(create_db_sql)
                logger.info("库 %s 就绪", db)
            except Exception as e:
                logger.error("建库失败 %s: %s", db, e)
                failures.append((db, "<database>", str(e)))
                continue

        # 查源库所有表
        tables = list_tables(source, db)
        logger.info("库 %s: %d 张表", db, len(tables))

        for table_name, engine in tables:
            total_tables += 1
            # 获取建表 DDL
            ddl = get_create_ddl(source, db, table_name)

            if args.dry_run:
                logger.info("[dry-run] %s.%s (%s)", db, table_name, engine)
                continue

            # 执行建表
            try:
                execute_on_target(target, ddl, db, table_name)
                total_ok += 1
            except Exception as e:
                failures.append((db, table_name, str(e)))
                if not args.skip_errors:
                    logger.error("遇到错误，终止（--skip-errors 可跳过继续）")
                    break
        if not args.skip_errors and failures:
            break

    # 汇总
    print("\n" + "=" * 50)
    print(f"同步完成：共 {total_tables} 张表，成功 {total_ok}，失败 {len(failures)}")
    if failures:
        print("\n失败明细：")
        for db, table, err in failures:
            print(f"  {db}.{table}: {err}")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(
        description="从源 ClickHouse 同步所有库表结构到目标 ClickHouse（仅 DDL）"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 DDL，不执行建库建表")
    parser.add_argument("--only", type=str,
                        help="只同步指定库（如 --only ODS）")
    parser.add_argument("--skip-errors", action="store_true",
                        help="单表建表失败时跳过继续，不中断")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
