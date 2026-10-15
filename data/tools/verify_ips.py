# 临时诊断脚本：核对生成的 Excel 中 (所属单位, IP) 是否落在 segments_cache 的网段内
import ipaddress
import json
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[2]   # data/tools/ 上两级 = 项目根
CACHE = ROOT / "output" / "segments_cache.json"
EXCEL = ROOT / "output" / "asset_excel" / "asset_app_001.xlsx"

with open(CACHE, encoding="utf-8") as f:
    cache = json.load(f)
unit_segments = {k.strip(): v for k, v in cache["unit_segments"].items()}


def ip_in(ip_str: str, unit: str) -> str | None:
    """返回命中的网段；未命中返回 None"""
    segs = unit_segments.get(unit.strip())
    if not segs:
        return "__UNIT_NOT_IN_CACHE__"
    try:
        ip_int = int(ipaddress.IPv4Address(ip_str))
    except ValueError:
        return "__INVALID_IP__"
    for seg in segs:
        if "-" not in seg:
            continue
        s, e = seg.split("-", 1)
        try:
            if int(ipaddress.IPv4Address(s.strip())) <= ip_int <= int(ipaddress.IPv4Address(e.strip())):
                return seg
        except ValueError:
            continue
    return None


wb = openpyxl.load_workbook(EXCEL, read_only=True)
ws = wb[wb.sheetnames[0]]
# 列：第9列=所属单位，第33列=IP地址
total = 0
bad = []
unit_not_in_cache = set()
for row in ws.iter_rows(min_row=3, values_only=True):
    unit, ips_col = row[8], row[32]
    if not unit or not ips_col:
        continue
    total += 1
    for ip_port in str(ips_col).split(","):
        ip_str = ip_port.split(":")[0].strip()
        hit = ip_in(ip_str, str(unit))
        if hit is None:
            bad.append((unit, ip_str))
        elif hit == "__UNIT_NOT_IN_CACHE__":
            unit_not_in_cache.add(unit.strip())
wb.close()

print(f"总数据行: {total}")
print(f"IP 不在所属单位缓存网段内的行数: {len(bad)}")
for u, i in bad[:15]:
    print(f"  {u} | {i}")
print(f"单位不在缓存中的行（IP 无从校验）: {len(unit_not_in_cache)} 种单位")
for u in list(unit_not_in_cache)[:15]:
    print(f"  {u}")
