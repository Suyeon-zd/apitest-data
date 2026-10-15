# data/temp_seeder.py
# 阶段3：从 ODS.resource_ledger 按批次回查配对键 → 生成"合法但不同"的差异数据
#         → 分批直插 ODS.resource_ledger_temp（全部在 ClickHouse）
#
# 与旧版差异：分页由 OFFSET 改为游标 (ips, resource_id)，避免百万行时 OFFSET 退化
#
# 独立性约定：仅依赖标准库 logging，不 import core.*
import logging
import random

from data.factory.ddl_parser import parse_create_table
from data.factory.value_generator import ValueGenerator, GenContext

logger = logging.getLogger(__name__)


class TempSeeder:
    """构造待更新资产数据并写入 resource_ledger_temp"""

    def __init__(self, ck_client, temp_ddl, pair_keys, diff_rules,
                 batch_prefix, expected_count=None, insert_batch_size=50000,
                 min_ratio=0.95, rebuild=False):
        self.ck = ck_client
        self.table, all_columns, _ = parse_create_table(temp_ddl)
        self.col_map = {c.name: c for c in all_columns}
        self.pair_keys = list(pair_keys)
        self.diff_rules = dict(diff_rules or {})
        self.batch_prefix = batch_prefix
        self.expected_count = expected_count
        self.insert_batch_size = int(insert_batch_size)
        self.min_ratio = float(min_ratio)
        self.rebuild = rebuild
        self._ips_mac = {}
        self.generator = ValueGenerator()

        self._validate_columns()
        insert_names = set(self.pair_keys) | set(self.diff_rules)
        self.insert_columns = [c.name for c in all_columns if c.name in insert_names]

    def run(self) -> int:
        """执行回查 + 配对插入，返回成功写入行数"""
        # ① 先清旧数据（可选）
        if self.rebuild:
            sql = (f"ALTER TABLE ODS.{self.table} DELETE "
                   f"WHERE asset_code LIKE '{self.batch_prefix}%'")
            self.ck.query(sql)
            logger.info("已提交 temp 旧数据删除（CK 异步执行）: 前缀 %s", self.batch_prefix)

        # ② 回查本批底账总数并做比例校验
        total = self._count_ledger()
        if total <= 0:
            raise RuntimeError(
                f"底账中没有前缀为 {self.batch_prefix} 的数据，请先完成 Excel 导入"
            )
        if self.expected_count:
            ratio = total / float(self.expected_count)
            if ratio < self.min_ratio:
                raise RuntimeError(
                    f"底账回查 {total} 行，仅为预期 {self.expected_count} 的 "
                    f"{ratio:.1%}（< {self.min_ratio:.0%}），已中止；"
                    f"请检查导入失败明细后再执行阶段3"
                )
        print(f"底账回查 {total} 行，开始配对构造待更新数据...")

        # ③ 游标分页：只驻留一批内存；ORDER BY (ips, resource_id) 保证 1:1
        inserted = 0
        last_cursor = None
        while True:
            ledger_rows = self._fetch_pairs_page(last_cursor, self.insert_batch_size)
            if not ledger_rows:
                break
            rows = [self._build_row(r) for r in ledger_rows]
            self.ck.insert_many(self.table, self.insert_columns, rows, database="ODS")
            inserted += len(rows)

            last = ledger_rows[-1]
            last_cursor = (last.get("ips"), last.get("resource_id"))
            logger.info("temp 写入进度: %d/%d", inserted, total)

            if len(ledger_rows) < self.insert_batch_size:
                # 最后一批不足 batch_size，说明已经取完
                break

        print(f"\n[OK] ODS.{self.table} 配对写入完成: {inserted} 行")
        return inserted

    def _build_row(self, ledger_row: dict) -> tuple:
        """按底账一行构造 temp 行：配对键照搬，其余字段合法差异生成"""
        ctx = GenContext()
        values = {}
        for name in self.insert_columns:
            if name in self.pair_keys:
                values[name] = ledger_row.get(name)

        for name, spec in self.diff_rules.items():
            if name == "mac":
                ips_val = ledger_row.get("ips")
                if ips_val in self._ips_mac:
                    values[name] = self._ips_mac[ips_val]
                else:
                    mac_val = self._gen_different(name, spec, ledger_row, ctx)
                    self._ips_mac[ips_val] = mac_val
                    values[name] = mac_val
            else:
                values[name] = self._gen_different(name, spec, ledger_row, ctx)

        return tuple(values[name] for name in self.insert_columns)

    def _gen_different(self, name, spec, ledger_row, ctx):
        """生成"合法但不同"的值"""
        col = self.col_map[name]
        original = ledger_row.get(name)

        if "diff_choice" in spec:
            options = [v for v in spec["diff_choice"] if v != original]
            if not options:
                raise ValueError(f"字段 {name}: 排除原值后没有可选的合法值，请补充选项")
            rule = {"fixed": _choice(options, spec.get("weights"))}
        else:
            rule = {k: v for k, v in spec.items() if k != "ensure_diff"}

        if "fixed" in rule or rule.get("null") is True:
            return self.generator.generate(col, rule, ctx)

        for _ in range(5):
            value = self.generator.generate(col, rule, ctx)
            if original is None or value != original:
                return value
        raise ValueError(f"字段 {name}: 多次生成仍与底账值相同，请调整差异规则")

    def _count_ledger(self) -> int:
        sql = (f"SELECT count() AS c FROM ODS.resource_ledger "
               f"WHERE asset_code LIKE '{self.batch_prefix}%'")
        rows = self.ck.query(sql)
        return int(rows[0]["c"]) if rows else 0

    def _fetch_pairs_page(self, last_cursor, limit) -> list:
        """按游标 (ips, resource_id) 分页拉取配对键，避免 OFFSET 退化"""
        cols = ", ".join(self.pair_keys)
        base_where = f"asset_code LIKE '{self.batch_prefix}%'"
        if last_cursor is not None:
            last_ips, last_rid = last_cursor
            # 转义单引号，防止 SQL 注入（ips 是业务字符串）
            ips_safe = str(last_ips).replace("'", "''") if last_ips is not None else ""
            if last_ips is None:
                # NULL 值的边界场景：只在非 NULL 值之后继续
                where = f"{base_where} AND ips IS NOT NULL"
            else:
                where = (f"{base_where} AND (ips > '{ips_safe}' "
                         f"OR (ips = '{ips_safe}' AND resource_id > {int(last_rid)}))")
        else:
            where = base_where

        sql = (f"SELECT {cols} FROM ODS.resource_ledger "
               f"WHERE {where} ORDER BY ips, resource_id LIMIT {int(limit)}")
        return self.ck.query(sql) or []

    def _validate_columns(self):
        for name in self.pair_keys:
            if name not in self.col_map:
                raise ValueError(f"配对键 '{name}' 不在 {self.table} 字段列表中")
        for name in self.diff_rules:
            if name not in self.col_map:
                raise ValueError(f"差异字段 '{name}' 不在 {self.table} 字段列表中")


def _choice(options, weights=None):
    if weights and len(weights) == len(options):
        return random.choices(options, weights=weights, k=1)[0]
    return random.choice(options)