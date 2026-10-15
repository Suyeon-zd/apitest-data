# data/factory/factory.py
# 数据构造编排器：DDL 解析 + 规则匹配 + 行上下文/数据池 + 逐行生成
import logging
import random

from data.factory.ddl_parser import parse_create_table, ColumnMeta
from data.factory.value_generator import ValueGenerator, GenContext

logger = logging.getLogger(__name__)

_FIELD_TYPE_MAP = {
    "string": ("string", "varchar"),
    "str": ("string", "varchar"),
    "int": ("int", "bigint"),
    "integer": ("int", "bigint"),
    "decimal": ("decimal", "decimal"),
    "float": ("decimal", "decimal"),
    "bool": ("bool", "tinyint"),
    "boolean": ("bool", "tinyint"),
    "date": ("date", "date"),
    "datetime": ("datetime", "datetime"),
    "array": ("array", "json"),
    "list": ("array", "json"),
    "uuid": ("uuid", "varchar"),
}


class DataFactory:
    """读建表语句/显式字段 → 解析字段约束 → 按自定义规则/Faker 生成数据行"""

    _UNIQUE_RETRY = 50

    def __init__(self, ddl_text: str = None, rules_config: dict = None, dialect: str = None,
                 locale: str = "zh_CN", db_manager=None, strict_validate: bool = False):
        self.rules_config = rules_config or {}
        self._strict_validate = strict_validate
        field_specs = self.rules_config.get("fields")

        if field_specs:
            self.table = self.rules_config.get("name") or "api_payload"
            self.dialect = dialect or "api"
            self.all_columns = self._columns_from_specs(field_specs)
        else:
            if not ddl_text or not ddl_text.strip():
                raise ValueError("建表语句文本为空（或通过 rules_config.fields 显式声明字段）")
            self.table, self.all_columns, self.dialect = parse_create_table(ddl_text, dialect)

        if self.rules_config.get("table"):
            self.table = self.rules_config["table"]
        self.rules = self.rules_config.get("rules", {}) or {}
        self.seed = self.rules_config.get("seed")
        self.unique_fields = list(self.rules_config.get("unique") or [])
        self.generator = ValueGenerator(locale=locale)

        self._col_map = {c.name: c for c in self.all_columns}
        self._validate_fields()
        self.pools = self._load_pools(db_manager) if not strict_validate else {}

        self._seeded = False
        self._ctx = None
        self._unique_seen = {}

        self.active_columns = self._resolve_active_columns()
        if not strict_validate:
            skipped = [c.name for c in self.all_columns if c not in self.active_columns]
            logger.info(
                f"表 {self.table}（{self.dialect}）解析完成：共 {len(self.all_columns)} 个字段，"
                f"实际插入 {len(self.active_columns)} 个"
                + (f"，跳过 {skipped}" if skipped else "")
                + (f"，数据池 {list(self.pools)}" if self.pools else "")
            )

    def _columns_from_specs(self, field_specs: list) -> list:
        columns = []
        for i, spec in enumerate(field_specs):
            if isinstance(spec, str):
                name, type_decl = spec, "string"
            elif isinstance(spec, dict) and spec.get("name"):
                name, type_decl = spec["name"], str(spec.get("type", "string")).lower()
            else:
                raise ValueError(f"fields 第 {i + 1} 项配置不合法：需为字符串或 {{name, type}} 字典")

            base_type, type_name = _FIELD_TYPE_MAP.get(type_decl, ("string", "varchar"))
            columns.append(ColumnMeta(
                name=name,
                raw_type=type_name,
                type_name=type_name,
                base_type=base_type,
                nullable=True,
                inner_type="string" if base_type == "array" else None,
            ))
        return columns

    def _validate_fields(self):
        """校验 rules / unique 引用的字段是否存在于表结构

        strict_validate=True 时：规则字段不存在 → 抛错（用于 --validate）
        strict_validate=False 时：规则字段不存在 → 警告后忽略（默认宽容）
        """
        col_names = {c.name for c in self.all_columns}
        missing = [c for c in self.rules if c not in col_names]
        if missing:
            msg = f"规则字段 {missing} 不在表 {self.table} 的字段列表中"
            if self._strict_validate:
                raise ValueError(msg)
            for c in missing:
                logger.warning("规则字段 '%s' 不在表 %s 的字段列表中，已忽略", c, self.table)

        for uf in self.unique_fields:
            if uf not in col_names:
                raise ValueError(f"unique 字段 '{uf}' 不在表 {self.table} 的字段列表中")

    def _load_pools(self, db_manager) -> dict:
        pools = {}
        for name, cfg in (self.rules_config.get("pools") or {}).items():
            if isinstance(cfg, list):
                values, source = list(cfg), "内联列表"
            elif isinstance(cfg, dict) and cfg.get("sql"):
                if db_manager is None:
                    raise ValueError(
                        f"数据池 '{name}' 需要 DB 查询，但未提供数据库连接"
                        f"（请检查 config/<env>.yaml 的 databases 配置）"
                    )
                client = db_manager.get(cfg.get("db"))
                field = cfg.get("field")
                if not field:
                    raise ValueError(f"数据池 '{name}' 缺少 field 字段（指定取查询结果的哪一列）")
                results = client.query(cfg["sql"], database=cfg.get("database"))
                values = [r[field] for r in results] if results else []
                source = f"SQL[{cfg.get('db')}]"
            else:
                raise ValueError(
                    f"数据池 '{name}' 配置不合法：支持内联列表（[a, b, c]）"
                    f"或查询配置（{{db: 别名, sql: ..., field: ...}}）"
                )
            if not values:
                raise ValueError(f"数据池 '{name}' 为空（{source}），请检查数据源或 SQL")
            pools[name] = values
            logger.info(f"数据池 '{name}' 就绪：{len(values)} 个值（{source}）")
        return pools

    def _resolve_active_columns(self):
        """决定哪些字段参与插入：
        - AUTO_INCREMENT 字段自动跳过（交给数据库自增）
        - CK MATERIALIZED / ALIAS 字段自动跳过（数据库自动计算，禁止 INSERT）
        - 自定义规则中 skip: true 的字段跳过（交给 DEFAULT）
        """
        active = []
        for col in self.all_columns:
            if col.auto_increment:
                continue
            if col.materialized or col.alias:  # 【解决插库时，不允许插入字段】
                continue
            rule = self.rules.get(col.name) or {}
            if rule.get("skip") is True:
                continue
            active.append(col)
        return active

    @property
    def column_names(self):
        return [c.name for c in self.active_columns]

    def _get_ctx(self) -> GenContext:
        if self._ctx is None:
            if self.seed is not None and not self._seeded:
                random.seed(self.seed)
                self.generator.fake.seed_instance(self.seed)
                self._seeded = True
            ctx = GenContext()
            ctx.pools = self.pools
            ctx.resolve = lambda name: self._resolve_field(name, ctx)
            self._ctx = ctx
        return self._ctx

    def iter_rows(self, count: int):
        """流式逐行生成数据（生成器），避免一次性驻留全部数据导致 OOM。

        与 build_rows 共用同一 GenContext：unique 判重、history、seed 行为完全一致，
        因此调用方可安全地一次性消费，也可分批多次消费（如 CSV/DB 按 batch_size 拉取）。
        """
        ctx = self._get_ctx()
        for i in range(count):
            ctx.row = {}
            ctx.row_index = i
            for col in self.active_columns:
                if col.name in ctx.row:
                    continue
                ctx.row[col.name] = self._gen_unique(col, ctx)
            self._record_history(ctx)
            yield tuple(ctx.row[c.name] for c in self.active_columns)

    def build_rows(self, count: int):
        """一次性生成 count 行（小批量/预览场景使用；大批量请用 iter_rows 流式消费）"""
        rows = list(self.iter_rows(count))
        logger.info(f"表 {self.table} 数据生成完成: {count} 行")
        return self.table, self.column_names, rows

    def _record_history(self, ctx: GenContext):
        for name, value in ctx.row.items():
            ctx.history.setdefault(name, []).append(value)
        for name in self.unique_fields:
            if name in ctx.row:
                self._remember_unique(name, ctx.row[name])

    def _remember_unique(self, name: str, value):
        """把唯一字段值加入判重集合；不可哈希时告警而非静默"""
        try:
            self._unique_seen.setdefault(name, set()).add(value)
        except TypeError:
            logger.warning(
                "唯一字段 %s 的值类型不可哈希（%s），已跳过判重",
                name, type(value).__name__,
            )

    def _resolve_field(self, name, ctx: GenContext):
        if name in ctx.row:
            return ctx.row[name]
        if name in ctx.resolving:
            raise ValueError(f"检测到字段循环引用: {name}，请检查 ref/switch 规则")
        col = self._col_map.get(name)
        if col is None:
            raise ValueError(f"引用字段 '{name}' 不在表 {self.table} 的字段列表中")
        ctx.resolving.add(name)
        try:
            ctx.row[name] = self.generator.generate(col, self.rules.get(name), ctx)
        finally:
            ctx.resolving.discard(name)
        return ctx.row[name]

    def _gen_unique(self, col: ColumnMeta, ctx: GenContext):
        if col.name not in self.unique_fields:
            return self.generator.generate(col, self.rules.get(col.name), ctx)
        seen = self._unique_seen.setdefault(col.name, set())
        for _ in range(self._UNIQUE_RETRY):
            value = self.generator.generate(col, self.rules.get(col.name), ctx)
            if value not in seen:
                self._remember_unique(col.name, value)
                return value
        raise ValueError(
            f"字段 {col.name} 无法在 {self._UNIQUE_RETRY} 次重试内生成唯一值，"
            f"请更换生成规则或扩大取值范围"
        )

    def preview(self, count: int = 3):
        return self.build_rows(count)

    # ---------- 分组造数 ----------
    def build_grouped_rows(self, count: int, grouping: dict = None):
        if not grouping or not grouping.get("fields"):
            raise ValueError("分组造数缺少 grouping.fields 配置")
        ctx = self._get_ctx()

        group_fields = list(grouping["fields"])
        sizes = list(grouping.get("sizes") or [1])
        weights = grouping.get("weights")
        for name in group_fields:
            if name not in self._col_map:
                raise ValueError(f"grouping.fields 中的 '{name}' 不在字段列表中")

        groups = []
        produced = 0
        while produced < count:
            size = random.choices(sizes, weights=weights, k=1)[0] if weights else random.choice(sizes)
            size = min(int(size), count - produced)

            ctx.row = {}
            for name in group_fields:
                ctx.row[name] = self._gen_unique(self._col_map[name], ctx)

            group_rows = []
            for _ in range(size):
                row_values = dict(ctx.row)
                ctx.row = row_values
                for col in self.active_columns:
                    if col.name in row_values:
                        continue
                    row_values[col.name] = self._gen_unique(col, ctx)
                group_rows.append(tuple(row_values[c.name] for c in self.active_columns))
                self._record_history(ctx)

            groups.append(group_rows)
            produced += size

        logger.info("表 %s 分组数据生成完成: %d 行 / %d 个组", self.table, produced, len(groups))
        return self.column_names, groups