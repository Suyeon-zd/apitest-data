# testcases/test_data_factory_unit.py
# data/ 造数核心单元测试（纯本地：不连库、不发请求、不读 .env）
#
# 覆盖三类高风险纯逻辑：
#   ① DDL 解析（MySQL / ClickHouse 方言、自增、MATERIALIZED / ALIAS）
#   ② DataFactory 行为（seed 可复现、unique 唯一、grouping 组内一致、流式 iter_rows 等价）
#   ③ 取值规则（GB 11643 身份证校验位、template 顺序号）
import re

import pytest

from data.factory.ddl_parser import parse_create_table
from data.factory.factory import DataFactory

# ============ 测试用 DDL ============

MYSQL_DDL = """
CREATE TABLE `t_demo` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `user_name` VARCHAR(50) NOT NULL,
  `age` INT DEFAULT NULL,
  `amount` DECIMAL(12,2) DEFAULT NULL,
  `created_at` DATETIME DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

CK_DDL = """
CREATE TABLE demo.ck_demo (
  id UInt64,
  name String,
  amount Decimal64(2),
  total UInt64 MATERIALIZED id * 2,
  name_upper String ALIAS upper(name)
) ENGINE = MergeTree ORDER BY id;
"""

# ============ 测试用 fields 声明（不依赖 DDL 的表定义） ============

BASE_FIELDS = [
    {"name": "id", "type": "int"},
    {"name": "user_name", "type": "string"},
    {"name": "city", "type": "string"},
]

BASE_RULES = {
    "id": {"sequence": {"start": 1, "step": 1}},
    "user_name": {"faker": "name"},
    "city": {"choice": ["北京", "上海", "广州"]},
}


def _make_factory(**overrides) -> DataFactory:
    """基于 BASE_FIELDS/BASE_RULES 构造 DataFactory，overrides 覆盖表级配置"""
    cfg = {"name": "unit_demo", "fields": BASE_FIELDS, "rules": BASE_RULES}
    cfg.update(overrides)
    return DataFactory(rules_config=cfg)


class TestDdlParser:
    """DDL 解析：字段名/类型/约束提取正确"""

    def test_mysql_columns_and_constraints(self):
        table, columns, dialect = parse_create_table(MYSQL_DDL)
        assert table == "t_demo", f"表名解析错误: {table}"
        assert dialect == "mysql", f"方言推断错误: {dialect}"
        assert [c.name for c in columns] == ["id", "user_name", "age", "amount", "created_at"], \
            f"字段列表解析错误: {[c.name for c in columns]}"

        by_name = {c.name: c for c in columns}
        assert by_name["id"].auto_increment is True, "AUTO_INCREMENT 未识别（会导致 id 被显式插入）"
        assert by_name["id"].is_primary_key is True, "主键未识别"
        assert by_name["id"].nullable is False, "NOT NULL 未识别"
        assert by_name["user_name"].base_type == "string", "varchar 应归一化为 string"
        assert by_name["user_name"].length == 50, "varchar 长度未解析"
        assert by_name["user_name"].nullable is False, "NOT NULL 未识别"
        assert by_name["age"].nullable is True, "DEFAULT NULL 应可空"
        assert by_name["amount"].base_type == "decimal", "decimal 应归一化为 decimal"
        assert (by_name["amount"].precision, by_name["amount"].scale) == (12, 2), \
            "DECIMAL(12,2) 精度/小数位解析错误"
        assert by_name["created_at"].base_type == "datetime", "datetime 应归一化为 datetime"

    def test_clickhouse_materialized_and_alias(self):
        table, columns, dialect = parse_create_table(CK_DDL)
        assert table == "ck_demo", f"带库名前缀的表名应取末段: {table}"
        assert dialect == "clickhouse", f"方言推断错误: {dialect}"

        by_name = {c.name: c for c in columns}
        assert by_name["amount"].base_type == "decimal", "Decimal64 应归一化为 decimal"
        assert by_name["amount"].precision == 18, "Decimal64 总精度应为 18"
        assert by_name["amount"].scale == 2, "Decimal64(2) 的 scale 应为 2"
        assert by_name["total"].materialized is True, "MATERIALIZED 未识别（CK 禁止显式插入）"
        assert by_name["name_upper"].alias is True, "ALIAS 未识别（CK 禁止显式插入）"


class TestDataFactory:
    """DataFactory：seed / unique / grouping / 流式生成"""

    def test_seed_makes_output_reproducible(self):
        rows_a = _make_factory(seed=42).build_rows(5)[2]
        rows_b = _make_factory(seed=42).build_rows(5)[2]
        assert rows_a == rows_b, "相同 seed + 相同配置应产出完全相同的数据集"

    def test_iter_rows_matches_build_rows(self):
        """流式 iter_rows 与一次性 build_rows 结果必须一致（seed 相同时）"""
        streamed = list(_make_factory(seed=7).iter_rows(5))
        bulk = _make_factory(seed=7).build_rows(5)[2]
        assert streamed == bulk, "iter_rows 与 build_rows 产出不一致，流式改造破坏了生成语义"

    def test_unique_field_has_no_duplicate(self):
        factory = DataFactory(rules_config={
            "name": "unit_unique",
            "fields": [{"name": "phone", "type": "string"}],
            "rules": {"phone": {"template": "138{random_int:8}"}},
            "unique": ["phone"],
        })
        _, columns, rows = factory.build_rows(30)
        idx = columns.index("phone")
        values = [r[idx] for r in rows]
        assert len(values) == 30, f"生成行数错误: {len(values)}"
        assert len(set(values)) == 30, f"unique 字段出现重复值: {values}"

    def test_grouping_keeps_group_fields_consistent(self):
        grouping = {"fields": ["asset_code"], "sizes": [3]}
        factory = DataFactory(rules_config={
            "name": "unit_group",
            "fields": [{"name": "asset_code", "type": "string"},
                       {"name": "owner", "type": "string"}],
            "rules": {"asset_code": {"template": "ZC-{seq:4}"},
                      "owner": {"faker": "name"}},
        })
        columns, groups = factory.build_grouped_rows(9, grouping)
        assert sum(len(g) for g in groups) == 9, "分组后总行数不等于请求行数"
        assert all(len(g) == 3 for g in groups), f"每组成员数应为 3: {[len(g) for g in groups]}"

        code_idx = columns.index("asset_code")
        codes = [g[0][code_idx] for g in groups]
        for group in groups:
            assert len({row[code_idx] for row in group}) == 1, \
                f"同一组内 asset_code 不一致: {group}"
        assert len(set(codes)) == len(codes), f"不同组的 asset_code 应互不相同: {codes}"

    def test_auto_increment_column_excluded_from_insert(self):
        """AUTO_INCREMENT 字段不得进入插入列（否则与数据库自增冲突）"""
        factory = DataFactory(ddl_text=MYSQL_DDL)
        assert "id" not in factory.column_names, "AUTO_INCREMENT 字段不应参与插入"
        assert "user_name" in factory.column_names, "普通字段应参与插入"

    def test_materialized_alias_columns_excluded_from_insert(self):
        factory = DataFactory(ddl_text=CK_DDL)
        assert "total" not in factory.column_names, "CK MATERIALIZED 字段不应参与插入"
        assert "name_upper" not in factory.column_names, "CK ALIAS 字段不应参与插入"

    def test_invalid_rule_field_raises_in_strict_mode(self):
        """严格校验模式（--validate 用）下规则字段写错必须报错，而非静默忽略"""
        with pytest.raises(ValueError, match="不在表"):
            DataFactory(
                rules_config={
                    "name": "unit_strict",
                    "fields": [{"name": "id", "type": "int"}],
                    "rules": {"not_exist": {"fixed": 1}},
                },
                strict_validate=True,
            )


class TestValueRules:
    """取值规则：身份证校验位、模板顺序号"""

    _WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    _CHECK = "10X98765432"

    def test_id_card_checksum_and_area(self):
        factory = DataFactory(rules_config={
            "name": "unit_idcard",
            "fields": [{"name": "id_card", "type": "string"}],
            "rules": {"id_card": {"id_card": {"area": "341111", "min_age": 20, "max_age": 40}}},
        })
        _, columns, rows = factory.build_rows(3)
        idx = columns.index("id_card")
        for row in rows:
            value = row[idx]
            assert re.fullmatch(r"\d{17}[\dX]", value), f"身份证格式错误: {value}"
            assert value.startswith("341111"), f"地区码未生效: {value}"
            total = sum(int(value[i]) * self._WEIGHTS[i] for i in range(17))
            assert value[17] == self._CHECK[total % 11], f"身份证校验位错误: {value}"

    def test_template_seq_placeholder_is_sequential(self):
        factory = DataFactory(rules_config={
            "name": "unit_tpl",
            "fields": [{"name": "code", "type": "string"}],
            "rules": {"code": {"template": "ZC-{seq:3}"}},
        })
        _, columns, rows = factory.build_rows(3)
        idx = columns.index("code")
        assert [r[idx] for r in rows] == ["ZC-001", "ZC-002", "ZC-003"], \
            f"{{seq:3}} 应生成补零顺序号: {[r[idx] for r in rows]}"
