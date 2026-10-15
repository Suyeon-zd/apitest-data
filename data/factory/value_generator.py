# data/factory/value_generator.py
# 字段值生成器：① Faker 按字段类型/约束随机生成；② 用户自定义规则生成（含字段关联）
import logging
import os
import re
import uuid
import random
import hashlib
from datetime import datetime, date, timedelta

from data.factory.ddl_parser import ColumnMeta

logger = logging.getLogger(__name__)


# MySQL 各类整数的取值范围（含无符号）
_MYSQL_INT_RANGES = {
    "tinyint":   (-128, 127, 0, 255),
    "smallint":  (-32768, 32767, 0, 65535),
    "mediumint": (-8388608, 8388607, 0, 16777215),
    "int":       (-2147483648, 2147483647, 0, 4294967295),
    "integer":   (-2147483648, 2147483647, 0, 4294967295),
    "bigint":    (-10 ** 12, 10 ** 12, 0, 10 ** 12),
    "bit":       (0, 1, 0, 1),
    "year":      (1970, 2030, 1970, 2030),
}

_CK_INT_RANGES = {
    "int8":  (-128, 127), "uint8":  (0, 255),
    "int16": (-32768, 32767), "uint16": (0, 65535),
    "int32": (-2147483648, 2147483647), "uint32": (0, 4294967295),
    "int64": (-10 ** 12, 10 ** 12), "uint64": (0, 10 ** 12),
    "int128": (-10 ** 12, 10 ** 12), "uint128": (0, 10 ** 12),
    "int256": (-10 ** 12, 10 ** 12), "uint256": (0, 10 ** 12),
}

_COLUMN_NAME_HINTS = {
    "phone": "phone_number", "mobile": "phone_number", "tel": "phone_number",
    "email": "email", "mail": "email",
    "url": "url", "website": "url",
    "address": "address", "addr": "address",
    "id_card": "ssn", "idcard": "ssn",
}

_TRANSFORMS = {"pinyin", "upper", "lower", "md5", "prefix", "suffix"}

_ID_CARD_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CARD_CHECK = "10X98765432"
_ID_CARD_AREAS = [
    "110101", "310110", "440103", "330106", "510107",
    "420106", "320105", "341111", "350203", "610113",
]


class GenContext:
    """单次造数运行的共享上下文"""
    __slots__ = ("row", "pools", "history", "row_index", "resolve", "resolving")

    def __init__(self):
        self.row = {}
        self.pools = {}
        self.history = {}
        self.row_index = 0
        self.resolve = None
        self.resolving = set()


class ValueGenerator:
    """按字段约束或自定义规则生成单个字段值"""

    def __init__(self, locale: str = "zh_CN"):
        try:
            from faker import Faker
        except ImportError as e:
            raise ImportError("数据生成依赖 faker，请先安装: pip install faker") from e
        self.fake = Faker(locale)
        self._seq_state = {}
        self._tpl_seq_state = {}

    # ---------- 对外入口 ----------
    def generate(self, col: ColumnMeta, rule: dict = None, ctx: GenContext = None):
        value = self._by_type(col)
        if rule:
            ruled = self._by_rule(col, rule, ctx)
            if ruled is not _NOT_HANDLED:
                value = ruled
            if rule.get("monotonic"):
                value = self._enforce_monotonic(col, value, ctx)
            if rule.get("transform"):
                value = self._apply_transform(col, value, rule["transform"])
        return value

    # ---------- ① 用户自定义规则 ----------
    def _by_rule(self, col: ColumnMeta, rule: dict, ctx: GenContext):
        if "fixed" in rule:
            return rule["fixed"]
        if "choice" in rule:
            choices = rule["choice"]
            weights = rule.get("weights")
            if weights:
                return random.choices(choices, weights=weights, k=1)[0]
            return random.choice(choices)
        if "range" in rule:
            low, high = rule["range"]
            return random.randint(int(low), int(high))
        if "sequence" in rule:
            seq_cfg = rule["sequence"] or {}
            start = seq_cfg.get("start", 1)
            step = seq_cfg.get("step", 1)
            current = self._seq_state.get(col.name, start)
            self._seq_state[col.name] = current + step
            return current
        if "ref" in rule:
            return self._lookup_field(col, rule["ref"], ctx)
        if "switch" in rule:
            cfg = rule["switch"] or {}
            # YAML 1.1 陷阱："on" 会被 PyYAML 解析为布尔 True，需要兼容
            on_field = cfg.get("on")
            if on_field is None and True in cfg:
                on_field = cfg[True]
                logger.warning(
                    "字段 %s: switch.on 被 YAML 1.1 解析为布尔键，已自动修复。"
                    "建议在 YAML 中写 \"on\"（带引号）", col.name,
                )
            on_value = self._lookup_field(col, on_field, ctx)
            cases = cfg.get("cases") or {}
            matched = cases.get(on_value, cases.get(str(on_value)))
            if matched is None:
                matched = cfg.get("default")
            if isinstance(matched, dict):
                return self.generate(col, matched, ctx)
            return matched
        if "from_pool" in rule:
            pool_name = rule["from_pool"]
            pool = (ctx.pools if ctx else {}).get(pool_name)
            if pool is None:
                raise ValueError(f"字段 {col.name}: 数据池 '{pool_name}' 未定义（请在表定义的 pools 中配置）")
            if not pool:
                raise ValueError(f"字段 {col.name}: 数据池 '{pool_name}' 为空")
            return random.choice(pool)
        if "self_ref" in rule:
            cfg = rule["self_ref"] or {}
            field = cfg.get("field") or col.name
            if ctx and ctx.resolve:
                ctx.resolve(field)
            history = (ctx.history if ctx else {}).get(field) or []
            if history:
                return random.choice(history)
            if "root" not in cfg:
                raise ValueError(f"字段 {col.name}: self_ref 首行无历史值，请在规则中指定 root（如 root: 0）")
            return cfg["root"]
        if "faker" in rule:
            method = getattr(self.fake, rule["faker"], None)
            if not method:
                raise ValueError(f"字段 {col.name}: Faker 不支持的方法 {rule['faker']!r}")
            return method(**(rule.get("faker_kwargs") or {}))
        if "id_card" in rule:
            return self._gen_id_card(rule["id_card"])
        if "ip_port" in rule:
            return self._gen_ip_port(rule["ip_port"])
        if "template" in rule:
            return self._render_template(rule["template"])
        if rule.get("null") is True:
            return None
        return _NOT_HANDLED

    # ---------- 字段引用与转换 ----------
    def _lookup_field(self, col: ColumnMeta, name, ctx: GenContext):
        if not name:
            raise ValueError(f"字段 {col.name}: ref/switch.on 缺少目标字段名")
        if ctx is None or ctx.resolve is None:
            raise ValueError("ref/switch 规则依赖行上下文，请通过 DataFactory 生成数据")
        return ctx.resolve(name)

    def _apply_transform(self, col: ColumnMeta, value, spec):
        if not spec:
            return value
        steps = spec if isinstance(spec, (list, tuple)) else [spec]
        for step in steps:
            name, _, arg = str(step).partition(":")
            if name not in _TRANSFORMS:
                raise ValueError(
                    f"字段 {col.name}: 不支持的 transform {step!r}（可用: "
                    f"pinyin/upper/lower/md5/prefix:xxx/suffix:xxx）"
                )
            if name == "pinyin":
                value = self._to_pinyin(col, value)
            elif name == "upper":
                value = str(value).upper()
            elif name == "lower":
                value = str(value).lower()
            elif name == "md5":
                value = hashlib.md5(str(value).encode("utf-8")).hexdigest()
            elif name == "prefix":
                value = f"{arg}{value}"
            else:
                value = f"{value}{arg}"
        return value

    def _to_pinyin(self, col: ColumnMeta, value):
        try:
            from pypinyin import lazy_pinyin
        except ImportError as e:
            raise ImportError(
                f"字段 {col.name}: transform: pinyin 需要 pypinyin，请先安装: pip install pypinyin"
            ) from e
        return "".join(lazy_pinyin(str(value)))

    def _enforce_monotonic(self, col: ColumnMeta, value, ctx: GenContext):
        if ctx is None or value is None:
            return value
        history = ctx.history.get(col.name) or []
        last = history[-1] if history else None
        if last is None:
            return value
        try:
            if value > last:
                return value
        except TypeError:
            return value
        if isinstance(value, datetime):
            return last + timedelta(seconds=random.randint(1, 600))
        if isinstance(value, date):
            return last + timedelta(days=random.randint(1, 30))
        if isinstance(value, int):
            return last + random.randint(1, 100)
        if isinstance(value, float):
            return round(last + random.uniform(0.01, 100), 2)
        return value

    def _render_template(self, template: str) -> str:
        def repl(m):
            token = m.group(1)
            if token == "uuid":
                return uuid.uuid4().hex
            if token == "word":
                return self.fake.word()
            if token == "name":
                return self.fake.name()
            if token == "phone":
                return self.fake.phone_number()
            if token == "email":
                return self.fake.email()
            if token == "random_int":
                return str(random.randint(0, 999999))
            if token.startswith("random_int:"):
                n = int(token.split(":", 1)[1])
                return str(random.randint(0, 10 ** n - 1)).zfill(n)
            if token == "seq" or token.startswith("seq:"):
                width = int(token.split(":", 1)[1]) if ":" in token else 0
                current = self._tpl_seq_state.get(template, 1)
                self._tpl_seq_state[template] = current + 1
                return str(current).zfill(width) if width else str(current)
            return m.group(0)

        return re.sub(r"\{([a-z_]+(?::\d+)?)\}", repl, template)

    def _gen_id_card(self, spec) -> str:
        spec = spec if isinstance(spec, dict) else {}
        area = spec.get("area")
        if area:
            area = str(area)[:6].ljust(6, "0")
        else:
            area = random.choice(_ID_CARD_AREAS)

        min_age = int(spec.get("min_age", 18))
        max_age = int(spec.get("max_age", 60))
        today = date.today()

        def _birth_at(age):
            try:
                return today.replace(year=today.year - age)
            except ValueError:
                return date(today.year - age, today.month, 28)

        start = _birth_at(max_age)
        end = _birth_at(min_age)
        birth = start + timedelta(days=random.randint(0, max((end - start).days, 0)))

        seq3 = str(random.randint(1, 999)).zfill(3)
        body = area + birth.strftime("%Y%m%d") + seq3
        total = sum(int(body[i]) * _ID_CARD_WEIGHTS[i] for i in range(17))
        check = _ID_CARD_CHECK[total % 11]
        return body + check

    def _gen_ip_port(self, spec) -> str:
        spec = spec if isinstance(spec, dict) else {}
        port_min = int(spec.get("port_min", 1024))
        port_max = int(spec.get("port_max", 65535))
        return f"{self.fake.ipv4()}:{random.randint(port_min, port_max)}"

    # ---------- ② 按字段类型/约束自动生成 ----------
    def _by_type(self, col: ColumnMeta):
        bt = col.base_type
        if bt == "int":
            return self._gen_int(col)
        if bt == "decimal":
            return self._gen_decimal(col)
        if bt == "bool":
            return self.fake.pybool()
        if bt == "string":
            return self._gen_string(col)
        if bt == "text":
            return self.fake.paragraph(nb_sentences=2).replace("\n", " ").replace("\r", " ")
        if bt == "date":
            return self.fake.date_between(start_date="-1y", end_date="today")
        if bt == "datetime":
            return self.fake.date_time_between(start_date="-1y", end_date="now")
        if bt == "time":
            return self.fake.time()
        if bt == "enum":
            return random.choice(col.enum_values) if col.enum_values else self.fake.word()
        if bt == "uuid":
            return str(uuid.uuid4())
        if bt == "json":
            return "{}"
        if bt == "binary":
            return os.urandom(min(col.length, 16) if col.length else 8)
        if bt == "array":
            return [self._gen_inner(col.inner_type) for _ in range(random.randint(1, 3))]
        return None if col.nullable else "test"

    def _gen_int(self, col: ColumnMeta):
        name = col.type_name
        if name in _MYSQL_INT_RANGES:
            lo_s, hi_s, lo_u, hi_u = _MYSQL_INT_RANGES[name]
            lo, hi = (lo_u, hi_u) if col.unsigned else (lo_s, hi_s)
            return random.randint(lo, hi)
        if name in _CK_INT_RANGES:
            lo, hi = _CK_INT_RANGES[name]
            return random.randint(lo, hi)
        return random.randint(0, 999999)

    def _gen_decimal(self, col: ColumnMeta):
        # 优先用 precision（与 length 语义解耦）；CK Decimal32/64/128/256 已在 parser 中填好
        if col.precision is not None:
            precision = col.precision
        elif col.length is not None:
            precision = col.length
        else:
            precision = 10
        scale = col.scale if col.scale is not None else 2
        left = max(1, min(precision - scale, 18))
        return self.fake.pydecimal(left_digits=left, right_digits=scale, positive=col.unsigned)

    def _gen_string(self, col: ColumnMeta):
        hint = _COLUMN_NAME_HINTS.get(col.name.lower())
        if hint:
            value = getattr(self.fake, hint)()
            if isinstance(value, str):
                value = value.replace("\n", " ")
            if col.length:
                value = str(value)[:col.length]
            return value

        if col.length:
            word = self.fake.word()
            if len(word) > col.length:
                return word[:col.length]
            return word
        return self.fake.word()

    def _gen_inner(self, inner_type: str):
        t = (inner_type or "string").lower()
        if "int" in t or "uint" in t:
            return random.randint(0, 1000)
        if "float" in t or "decimal" in t:
            return round(random.uniform(0, 1000), 2)
        if "date" in t and "time" in t:
            return self.fake.date_time_between(start_date="-1y", end_date="now")
        if "date" in t:
            return self.fake.date_between(start_date="-1y", end_date="today")
        return self.fake.word()


_NOT_HANDLED = object()