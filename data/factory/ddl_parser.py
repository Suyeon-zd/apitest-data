# data/factory/ddl_parser.py
# 解析建表语句（兼容 MySQL 和 ClickHouse），提取字段名、类型与约束
import re
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class ColumnMeta:
    """单个字段的元信息（由建表语句解析得到）"""
    name: str
    raw_type: str                       # 原始类型，如 varchar(100) / Nullable(Decimal(12,2))
    type_name: str = ""                 # 去掉括号参数的类型名（小写），如 varchar / uint64
    base_type: str = "unknown"          # 归一化大类
    length: Optional[int] = None        # 长度（MySQL varchar(n) / Decimal(P) 的 P）
    scale: Optional[int] = None         # 小数位（Decimal(P,S) 的 S）
    precision: Optional[int] = None     # 总精度（Decimal 类型，与 length 语义解耦）
    nullable: bool = True
    is_primary_key: bool = False
    auto_increment: bool = False
    has_default: bool = False
    unsigned: bool = False
    materialized: bool = False  # 【新增】MATERIALIZED 列（CK，禁止 INSERT）
    alias: bool = False  # 【新增】ALIAS 列（CK，禁止 INSERT）
    enum_values: List[str] = field(default_factory=list)
    inner_type: Optional[str] = None


_TABLE_LEVEL_KEYWORDS = (
    "PRIMARY KEY", "UNIQUE KEY", "UNIQUE INDEX", "FULLTEXT KEY", "FULLTEXT INDEX",
    "SPATIAL KEY", "FOREIGN KEY", "CONSTRAINT", "KEY ", "INDEX ", "CHECK",
    "PARTITION BY", "ORDER BY", "SAMPLE BY", "TTL ", "PROJECTION",
)


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    lines = []
    for line in sql.splitlines():
        line = re.sub(r"(?:\s+|^)--.*$", "", line)
        line = re.sub(r"(?:\s+|^)#.*$", "", line)
        lines.append(line)
    return "\n".join(lines)


def _find_balanced_body(sql: str, start: int):
    depth = 0
    i = start
    in_quote = None
    while i < len(sql):
        ch = sql[i]
        if in_quote:
            if ch == in_quote:
                if i + 1 < len(sql) and sql[i + 1] == in_quote:
                    i += 2
                    continue
                in_quote = None
        else:
            if ch in ("'", '"'):
                in_quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return sql[start + 1:i], i
        i += 1
    raise ValueError("建表语句括号不匹配，未找到与 '(' 对应的 ')'")


def _split_top_level(body: str) -> List[str]:
    parts = []
    depth = 0
    buf = []
    in_quote = None
    for ch in body:
        if in_quote:
            buf.append(ch)
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ("'", '"'):
            in_quote = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return parts


def _extract_type(rest: str):
    m = re.match(r"([A-Za-z0-9_]+)", rest)
    if not m:
        return "", rest
    type_end = m.end()
    j = type_end
    while j < len(rest) and rest[j].isspace():
        j += 1
    if j < len(rest) and rest[j] == "(":
        depth = 0
        k = j
        in_quote = None
        while k < len(rest):
            ch = rest[k]
            if in_quote:
                if ch == in_quote:
                    in_quote = None
            elif ch in ("'", '"'):
                in_quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    type_end = k + 1
                    break
            k += 1
    full_type = rest[:type_end].strip()
    modifiers = rest[type_end:].strip()
    return full_type, modifiers


# ---------- 类型归一化 ----------

_MYSQL_INT_TYPES = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}
_CK_INT_TYPES = {
    "uint8", "uint16", "uint32", "uint64", "uint128", "uint256",
    "int8", "int16", "int32", "int64", "int128", "int256",
}
# CK Decimal32/64/128/256 的总精度（固定）；括号里是 scale
_CK_DECIMAL_TOTAL_PRECISION = {
    "decimal32": 9, "decimal64": 18, "decimal128": 38, "decimal256": 76,
}


def _extract_enum_values(raw_type: str) -> List[str]:
    return re.findall(r"'((?:[^']|'')*)'", raw_type)


def _classify_mysql(raw_type: str, col: ColumnMeta):
    m = re.match(r"[A-Za-z0-9_]+", raw_type)
    type_name = m.group(0).lower() if m else "unknown"
    col.type_name = type_name

    if type_name in _MYSQL_INT_TYPES or type_name == "bit":
        col.base_type = "int"
    elif type_name in ("bool", "boolean"):
        col.base_type = "bool"
    elif type_name in ("float", "double", "real", "decimal", "numeric"):
        col.base_type = "decimal"
    elif type_name in ("char", "varchar", "nvarchar", "varchar2"):
        col.base_type = "string"
        m = re.search(r"\((\d+)", raw_type)
        if m:
            col.length = int(m.group(1))
    elif type_name in ("tinytext", "mediumtext", "longtext", "text"):
        col.base_type = "text"
    elif type_name == "json":
        col.base_type = "json"
    elif type_name in ("enum", "set"):
        col.base_type = "enum"
        col.enum_values = _extract_enum_values(raw_type)
    elif type_name == "date":
        col.base_type = "date"
    elif type_name in ("datetime", "timestamp"):
        col.base_type = "datetime"
    elif type_name == "time":
        col.base_type = "time"
    elif type_name == "year":
        col.base_type = "int"
    elif type_name in ("binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"):
        col.base_type = "binary"
        m = re.search(r"\((\d+)", raw_type)
        if m:
            col.length = int(m.group(1))
    else:
        col.base_type = "string"  # 未知类型兜底按字符串处理

    # decimal(P,S)：显式设置 precision
    if col.base_type == "decimal":
        m = re.search(r"\((\d+)(?:\s*,\s*(\d+))?\)", raw_type)
        if m:
            col.precision = int(m.group(1))
            col.length = col.precision        # 向后兼容旧字段
            col.scale = int(m.group(2)) if m.group(2) else 0


def _unwrap_ck(raw_type: str):
    nullable = False
    changed = True
    while changed:
        changed = False
        m = re.match(r"Nullable\((.*)\)$", raw_type, flags=re.IGNORECASE | re.DOTALL)
        if m:
            raw_type = m.group(1).strip()
            nullable = True
            changed = True
        m = re.match(r"LowCardinality\((.*)\)$", raw_type, flags=re.IGNORECASE | re.DOTALL)
        if m:
            raw_type = m.group(1).strip()
            changed = True
        m = re.match(r"SimpleAggregateFunction\([^,]+,\s*(.*)\)$", raw_type, flags=re.IGNORECASE | re.DOTALL)
        if m:
            raw_type = m.group(1).strip()
            changed = True
    return raw_type, nullable


def _classify_clickhouse(raw_type: str, col: ColumnMeta):
    inner, was_nullable = _unwrap_ck(raw_type)
    if was_nullable:
        col.nullable = True

    # Array(T)
    m = re.match(r"Array\((.*)\)$", inner, flags=re.IGNORECASE | re.DOTALL)
    if m:
        col.base_type = "array"
        inner_str, _ = _unwrap_ck(m.group(1).strip())
        m2 = re.match(r"[A-Za-z0-9_]+", inner_str)
        col.inner_type = m2.group(0).lower() if m2 else "string"
        return

    # Map(K,V) 按 json 处理
    if re.match(r"Map\(", inner, flags=re.IGNORECASE):
        col.base_type = "json"
        return

    m = re.match(r"[A-Za-z0-9_]+", inner)
    type_name = m.group(0).lower() if m else "unknown"
    col.type_name = type_name

    if type_name in _CK_INT_TYPES:
        col.base_type = "bool" if type_name in ("bool", "boolean") else "int"
    elif type_name in ("bool", "boolean"):
        col.base_type = "bool"
    elif type_name in ("float32", "float64", "decimal",
                       "decimal32", "decimal64", "decimal128", "decimal256"):
        col.base_type = "decimal"
        # Decimal32(S) / Decimal64(S) / Decimal128(S) / Decimal256(S)
        #   → 括号里是 scale；总精度由类型固定
        if type_name in _CK_DECIMAL_TOTAL_PRECISION:
            col.precision = _CK_DECIMAL_TOTAL_PRECISION[type_name]
            m2 = re.search(r"\((\d+)\)", inner)
            col.scale = int(m2.group(1)) if m2 else 0
            col.length = col.scale        # 向后兼容旧字段
        else:
            # 通用 Decimal(P,S) / Decimal(P)
            m2 = re.search(r"\((\d+)(?:\s*,\s*(\d+))?\)", inner)
            if m2:
                col.precision = int(m2.group(1))
                col.scale = int(m2.group(2)) if m2.group(2) else 0
                col.length = col.precision
    elif type_name in ("string", "fixedstring"):
        col.base_type = "string"
        m2 = re.search(r"\((\d+)\)", inner)
        if m2:
            col.length = int(m2.group(1))
    elif type_name in ("date", "date32"):
        col.base_type = "date"
    elif type_name in ("datetime", "datetime64", "datetime32"):
        col.base_type = "datetime"
    elif type_name == "uuid":
        col.base_type = "uuid"
    elif type_name in ("enum8", "enum16"):
        col.base_type = "enum"
        col.enum_values = _extract_enum_values(inner)
    elif type_name == "json":
        col.base_type = "json"
    elif type_name in ("ipv4", "ipv6"):
        col.base_type = "string"
    elif type_name in ("nothing", "null"):
        col.base_type = "unknown"
        col.nullable = True
    else:
        col.base_type = "string"


def _infer_dialect(sql: str) -> str:
    """根据建表语句特征推断方言（data 层唯一实现）"""
    low = sql.lower()
    ck_signals = ("mergetree", "distributed", "replacingmergetree", "summingmergetree",
                  "aggregatingmergetree", "lowcardinality(", "uint64", "uint32",
                  "datetime64", "fixedstring", "order by (")
    mysql_signals = ("auto_increment", "engine=innodb", "engine = innodb", "tinyint",
                     "mediumtext", "longtext", "unsigned")
    ck_score = sum(1 for s in ck_signals if s in low)
    mysql_score = sum(1 for s in mysql_signals if s in low)
    if ck_score > mysql_score:
        return "clickhouse"
    if mysql_score > ck_score:
        return "mysql"
    return "mysql"


def parse_create_table(sql: str, dialect: Optional[str] = None):
    """解析建表语句。返回 (表名, [ColumnMeta, ...], 实际使用的方言)。"""
    sql = _strip_comments(sql)
    if dialect is None:
        dialect = _infer_dialect(sql)

    head = re.search(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?([A-Za-z0-9_.$]+)[`\"\]]?\s*\(",
        sql, flags=re.IGNORECASE,
    )
    if not head:
        raise ValueError("未找到 CREATE TABLE 语句")
    table_name = head.group(1).split(".")[-1]
    body, _ = _find_balanced_body(sql, head.end() - 1)

    columns: List[ColumnMeta] = []
    table_pk_cols = set()

    for part in _split_top_level(body):
        if not part:
            continue
        upper = part.upper().lstrip()
        if upper.startswith(_TABLE_LEVEL_KEYWORDS):
            pk_match = re.match(r"PRIMARY\s+KEY\s*\(([^)]+)\)", part, flags=re.IGNORECASE)
            if pk_match:
                for c in pk_match.group(1).split(","):
                    table_pk_cols.add(c.strip().strip("`\"' "))
            continue

        col_match = re.match(r"[`\"\[]?([A-Za-z0-9_$]+)[`\"\]]?\s+(.*)$", part, flags=re.DOTALL)
        if not col_match:
            continue
        col_name = col_match.group(1)
        rest = col_match.group(2).strip()
        raw_type, modifiers = _extract_type(rest)
        if not raw_type:
            continue

        col = ColumnMeta(name=col_name, raw_type=raw_type)

        if dialect == "clickhouse":
            col.nullable = False
            _classify_clickhouse(raw_type, col)
        else:
            col.nullable = not bool(re.search(r"\bNOT\s+NULL\b", modifiers, flags=re.IGNORECASE))
            _classify_mysql(raw_type, col)

        col.is_primary_key = bool(re.search(r"\bPRIMARY\s+KEY\b", modifiers, flags=re.IGNORECASE))
        col.auto_increment = bool(re.search(r"\bAUTO_INCREMENT\b", modifiers, flags=re.IGNORECASE))
        col.has_default = bool(re.search(r"\bDEFAULT\b", modifiers, flags=re.IGNORECASE))
        col.unsigned = bool(re.search(r"\bUNSIGNED\b", modifiers, flags=re.IGNORECASE))
        col.materialized = bool(re.search(r"\bMATERIALIZED\b", modifiers, flags=re.IGNORECASE))
        col.alias = bool(re.search(r"\bALIAS\b", modifiers, flags=re.IGNORECASE))

        columns.append(col)

    for col in columns:
        if col.name in table_pk_cols:
            col.is_primary_key = True
            col.nullable = False

    return table_name, columns, dialect