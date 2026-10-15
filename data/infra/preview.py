# data/preview.py
# 预览格式化：竖版/横版/值分布统计
import json
import datetime as _dt
from collections import Counter

_NARROW_RANGES = [(0x0020, 0x007E)]   # 基本可打印 ASCII 视为窄字符
_PREVIEW_LAYOUT_THRESHOLD = 8          # 列数 > 8 用竖版


def _display_width(s: str) -> int:
    return sum(1 if any(lo <= ord(ch) <= hi for lo, hi in _NARROW_RANGES) else 2 for ch in s)


def _pad_to_width(s: str, width: int) -> str:
    return s + " " * max(width - _display_width(s), 0)


def _format_value(v, max_len: int = 24) -> str:
    """None → NULL；'' → (空串)；datetime/date 格式化；超长截断"""
    if v is None:
        return "NULL"
    if v == "":
        return "(空串)"
    if isinstance(v, _dt.datetime):
        s = v.strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(v, _dt.date):
        s = v.strftime("%Y-%m-%d")
    elif isinstance(v, (bytes, bytearray)):
        s = v.hex()
    else:
        s = str(v)
    if _display_width(s) > max_len:
        target = max_len - 3
        chars, cur = [], 0
        for ch in s:
            w = 1 if any(lo <= ord(ch) <= hi for lo, hi in _NARROW_RANGES) else 2
            if cur + w > target:
                break
            chars.append(ch)
            cur += w
        s = "".join(chars) + "..."
    return s


def preview_vertical(columns, rows, max_value_len: int = 24):
    name_col_w = max((_display_width(c) for c in columns), default=8)
    cell_w = max_value_len + 3
    for i, col in enumerate(columns):
        values_part = "  │ ".join(
            _pad_to_width(_format_value(r[i], max_value_len), cell_w) for r in rows
        )
        print(f"  {_pad_to_width(col, name_col_w)}  │ {values_part}")
    tail = "  第{0}行".format(" / ".join(str(i) for i in range(1, len(rows) + 1)))
    print(f"  {'─' * (name_col_w + 2)}┼ {tail}")


def preview_table(columns, rows, max_value_len: int = 24):
    vw = max_value_len + 3
    header_left = "  字段"
    print(f"\n{header_left}  " + "  ".join(_pad_to_width(c, vw) for c in columns))
    print("  " + "-" * (len(header_left) + 2 + len(columns) * (vw + 2)))
    for i, row in enumerate(rows, 1):
        print(f"  第{i}行  " + "  ".join(
            _pad_to_width(_format_value(v, max_value_len), vw) for v in row
        ))


def preview_stats(columns, rows, rules_config=None, top_n: int = 5):
    """显式配了 choice/fixed 的字段值分布；行数 < 5 时跳过"""
    if len(rows) < 5:
        return
    rules = rules_config or {}
    interesting = {
        col for col, rule in rules.items()
        if isinstance(rule, dict) and ("choice" in rule or "fixed" in rule)
    }
    if not interesting:
        return
    print("\n  字段值分布（显式规则字段）:")

    def _stat_key(v):
        if isinstance(v, (list, dict)):
            return json.dumps(v, ensure_ascii=False)
        return v

    for col in columns:
        if col not in interesting:
            continue
        idx = columns.index(col)
        counter = Counter(_stat_key(r[idx]) for r in rows)
        total = sum(counter.values())
        top = counter.most_common(top_n)
        parts = ", ".join(f"{v!r}: {c} ({c/total*100:.0f}%)" for v, c in top)
        if len(counter) > top_n:
            parts += f", ...等{len(counter)}种"
        print(f"    {col}: {parts}")


def preview_data(columns, rows, rules_config=None, fmt: str = "auto", max_len: int = 24):
    """统一入口：auto 按列数自动选择，或显式 vertical/table"""
    use_vertical = fmt == "vertical" or (fmt == "auto" and len(columns) > _PREVIEW_LAYOUT_THRESHOLD)
    if use_vertical:
        preview_vertical(columns, rows, max_len)
    else:
        preview_table(columns, rows, max_len)
    preview_stats(columns, rows, rules_config)