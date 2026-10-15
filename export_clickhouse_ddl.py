#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出 ClickHouse 全部数据库 / 表的建表语句 (DDL)。

依赖:
    pip install clickhouse-driver

用法示例:
    # 导出全部（排除系统库）到单个文件
    python export_clickhouse_ddl.py --host 127.0.0.1 --port 9000 \
        --user default --password '' --output clickhouse_ddl.sql

    # 只导出指定库
    python export_clickhouse_ddl.py --databases db1 db2

    # 每个库输出一个独立的 .sql 文件
    python export_clickhouse_ddl.py --per-database --output ./ddl_out

    # 包含 system 库
    python export_clickhouse_ddl.py --include-system

    # 不生成 DROP 语句
    python export_clickhouse_ddl.py --no-drop
"""

import argparse
import logging
import os
import sys
from datetime import datetime

try:
    from clickhouse_driver import Client
except ImportError:
    sys.exit("缺少依赖，请先执行:  pip install clickhouse-driver")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ck-ddl")

# 默认排除的系统库
DEFAULT_EXCLUDE_DBS = {"system", "INFORMATION_SCHEMA", "information_schema"}

# 建表顺序权重：字典/普通表 -> 视图 -> 物化视图（物化视图依赖源表，必须最后建）
ENGINE_ORDER = {
    "Dictionary": 0,
    "View": 2,
    "MaterializedView": 3,
    "LiveView": 3,
    "WindowView": 3,
}

CREATE_KEYWORDS = (
    "CREATE MATERIALIZED VIEW",
    "CREATE WINDOW VIEW",
    "CREATE LIVE VIEW",
    "CREATE DICTIONARY",
    "CREATE TABLE",
    "CREATE VIEW",
)


def qi(name: str) -> str:
    """用反引号包裹标识符"""
    return "`{}`".format(name.replace("`", "``"))


def _inject_if_not_exists(ddl: str, keyword: str) -> str:
    """把 IF NOT EXISTS 插入到 CREATE XXX 之后"""
    upper = ddl.upper()
    idx = upper.find(keyword)
    if idx == -1:
        return ddl
    tail = upper[idx: idx + len(keyword) + 25]
    if "IF NOT EXISTS" in tail:
        return ddl
    return ddl[: idx + len(keyword)] + " IF NOT EXISTS" + ddl[idx + len(keyword):]


def get_create_database(client, db: str) -> str:
    rows = client.execute("SHOW CREATE DATABASE {}".format(qi(db)))
    ddl = rows[0][0].strip().rstrip(";")
    return _inject_if_not_exists(ddl, "CREATE DATABASE") + ";"


def get_create_table(client, db: str, table: str) -> str:
    rows = client.execute("SHOW CREATE TABLE {}.{}".format(qi(db), qi(table)))
    ddl = rows[0][0].strip().rstrip(";")
    for kw in CREATE_KEYWORDS:
        if ddl.upper().startswith(kw):
            ddl = _inject_if_not_exists(ddl, kw)
            break
    return ddl + ";"


def fetch_databases(client, include, exclude, include_system):
    rows = client.execute("SELECT name FROM system.databases ORDER BY name")
    dbs = [r[0] for r in rows]
    if include:
        inc = set(include)
        dbs = [d for d in dbs if d in inc]
    if not include_system:
        dbs = [d for d in dbs if d not in DEFAULT_EXCLUDE_DBS]
    exc = set(exclude or [])
    return [d for d in dbs if d not in exc]


def fetch_tables(client, db: str):
    """返回 [(table_name, engine), ...]，已按建表依赖顺序排序"""
    rows = client.execute(
        """
        SELECT name, engine
        FROM system.tables
        WHERE database = %(db)s AND is_temporary = 0
        """,
        {"db": db},
    )
    tables = [(r[0], r[1]) for r in rows]
    tables.sort(key=lambda x: (ENGINE_ORDER.get(x[1], 1), x[0]))
    return tables


def build_header(scope: str) -> list:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bar = "-- " + "=" * 72
    return [
        bar,
        "-- ClickHouse DDL dump  ({})".format(scope),
        "-- Generated at: {}".format(ts),
        bar,
        "",
    ]


def build_database_sql(client, db: str, args) -> list:
    lines = []
    bar = "-- " + "-" * 72
    lines.append(bar)
    lines.append("-- Database: {}".format(db))
    lines.append(bar)

    if not args.no_drop:
        lines.append("DROP DATABASE IF EXISTS {};".format(qi(db)))
    try:
        lines.append(get_create_database(client, db))
    except Exception as e:  # noqa: BLE001
        log.error("获取数据库 %s 的 DDL 失败: %s", db, e)
        lines.append("-- !! 导出数据库 DDL 失败: {}".format(e))
    lines.append("")

    tables = fetch_tables(client, db)
    if not tables:
        lines.append("-- (该库没有表)")
        lines.append("")
        return lines

    for name, engine in tables:
        lines.append("-- Table: {}.{}  [engine={}]".format(db, name, engine))
        if not args.no_drop:
            lines.append("DROP TABLE IF EXISTS {}.{};".format(qi(db), qi(name)))
        try:
            lines.append(get_create_table(client, db, name))
        except Exception as e:  # noqa: BLE001
            log.error("获取 %s.%s 的 DDL 失败: %s", db, name, e)
            lines.append("-- !! 导出失败: {}".format(e))
        lines.append("")

    return lines


def export(args):
    client = Client(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        connect_timeout=args.timeout,
        send_receive_timeout=args.timeout,
    )
    client.execute("SELECT 1")
    log.info("已连接 ClickHouse %s:%s", args.host, args.port)

    dbs = fetch_databases(
        client, args.databases, args.exclude_databases, args.include_system
    )
    if not dbs:
        log.warning("没有匹配到任何数据库，退出")
        return
    log.info("待导出数据库共 %d 个: %s", len(dbs), ", ".join(dbs))

    # ---- 每个库一个文件 ----
    if args.per_database:
        out_dir = args.output
        os.makedirs(out_dir, exist_ok=True)
        for db in dbs:
            lines = build_header("database `{}`".format(db))
            lines += build_database_sql(client, db, args)
            path = os.path.join(out_dir, "{}.sql".format(db.replace("/", "_")))
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines).rstrip() + "\n")
            log.info("已写入 %s", path)
        return

    # ---- 全部合并到一个文件 ----
    lines = build_header("all databases")
    for db in dbs:
        lines += build_database_sql(client, db, args)

    out_path = args.output
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    log.info("已写入 %s", out_path)


def main():
    p = argparse.ArgumentParser(
        description="导出 ClickHouse 所有库表的建表语句 (DDL)"
    )
    p.add_argument("--host", default="localhost", help="ClickHouse 主机 (默认 localhost)")
    p.add_argument("--port", type=int, default=9000, help="TCP 端口 (默认 9000)")
    p.add_argument("--user", default="default", help="用户名")
    p.add_argument("--password", default="", help="密码")
    p.add_argument("--database", default="default", help="连接时使用的库")
    p.add_argument("--timeout", type=int, default=300, help="超时秒数")
    p.add_argument("--output", default="clickhouse_ddl.sql",
                   help="输出文件；配合 --per-database 时为输出目录")
    p.add_argument("--per-database", action="store_true",
                   help="每个数据库单独输出一个 .sql 文件")
    p.add_argument("--databases", nargs="*", default=None,
                   help="只导出这些库（可多个）")
    p.add_argument("--exclude-databases", nargs="*", default=None,
                   help="排除这些库")
    p.add_argument("--include-system", action="store_true",
                   help="包含 system / INFORMATION_SCHEMA 等系统库")
    p.add_argument("--no-drop", action="store_true",
                   help="不生成 DROP DATABASE / DROP TABLE 语句")
    args = p.parse_args()
    export(args)


if __name__ == "__main__":
    main()