# 临时诊断脚本（纯本地，不连网）：分析 segments_cache 中各单位网段的重叠度
# 目的：验证「查询接口 deptId 过滤未生效，缓存里存的是全库网段」这一假设
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # data/tools/ 上两级 = 项目根
CACHE = ROOT / "output" / "segments_cache.json"

with open(CACHE, encoding="utf-8") as f:
    data = json.load(f)
us = data.get("unit_segments", {})
dept_ids = data.get("unit_dept_ids", {})

# ---------- ① 基本面 ----------
print("=" * 70)
print(f"缓存单位数: {len(us)}  |  unit_dept_ids 数: {len(dept_ids)}")

# 每单位网段条数分布
counts = sorted((len(segs) for segs in us.values()), reverse=True)
print(f"每单位网段条数: 最多 {counts[0]}，最少 {counts[-1]}，中位数 {counts[len(counts)//2]}")
hist = Counter(len(segs) for segs in us.values())
print(f"网段条数分布: {dict(sorted(hist.items()))}")

# ---------- ② 网段 → 持有单位集合（核心：重叠度） ----------
seg_owners = {}
for unit, segs in us.items():
    for s in segs:
        seg_owners.setdefault(s, set()).add(unit)

total_segs = len(seg_owners)
multi = {s: owners for s, owners in seg_owners.items() if len(owners) > 1}
print("=" * 70)
print(f"缓存中出现的网段总数（去重后）: {total_segs}")
print(f"被 ≥2 个单位共享的网段数: {len(multi)}  "
      f"({len(multi)/total_segs*100:.1f}%)" if total_segs else "")

if multi:
    print("\n共享网段 TOP20（按持有单位数排序）:")
    for s, owners in sorted(multi.items(), key=lambda x: -len(x[1]))[:20]:
        print(f"  {s}  ->  {len(owners)} 个单位")

# ---------- ③ 列表完全相同的单位组（查询不过滤的铁证） ----------
sig_map = {}
for unit, segs in us.items():
    sig_map.setdefault(tuple(sorted(segs)), []).append(unit)
same_groups = [g for g in sig_map.values() if len(g) > 1]
print("=" * 70)
print(f"网段列表【完全相同】的单位组数: {len(same_groups)}")
for g in same_groups[:10]:
    print(f"  {len(g)} 个单位列表一致: {g[:5]}{'...' if len(g) > 5 else ''}")

# ---------- ④ Excel 中 IP 的落点：落在共享网段 vs 独占网段 ----------
import ipaddress
import openpyxl

EXCEL = ROOT / "output" / "asset_excel" / "asset_app_001.xlsx"
wb = openpyxl.load_workbook(EXCEL, read_only=True)
ws = wb[wb.sheetnames[0]]


def seg_of(ip_str: str):
    """返回该 IP 命中的网段列表"""
    try:
        ip_int = int(ipaddress.IPv4Address(ip_str))
    except ValueError:
        return []
    hits = []
    for seg in seg_owners:
        if "-" not in seg:
            continue
        s, e = seg.split("-", 1)
        try:
            if int(ipaddress.IPv4Address(s.strip())) <= ip_int <= int(ipaddress.IPv4Address(e.strip())):
                hits.append(seg)
        except ValueError:
            continue
    return hits


total_rows = 0
shared_rows = 0      # IP 落在被多单位共享的网段上
solo_rows = 0        # IP 落在仅所属单位持有的网段上（真正安全）
other_only = 0       # IP 不落在任何缓存网段（理论上应为 0，前面已验证）
bad_units = Counter()
for row in ws.iter_rows(min_row=3, values_only=True):
    unit, ips_col = row[8], row[32]
    if not unit or not ips_col:
        continue
    total_rows += 1
    for ip_port in str(ips_col).split(","):
        ip_str = ip_port.split(":")[0].strip()
        hits = seg_of(ip_str)
        if not hits:
            other_only += 1
            bad_units[unit] += 1
            continue
        # 该 IP 命中的网段是否全部被多单位共享？
        if all(len(seg_owners[h]) > 1 for h in hits):
            shared_rows += 1
        else:
            solo_rows += 1
wb.close()

print("=" * 70)
print(f"Excel 数据行: {total_rows}")
print(f"  IP 落在【仅所属单位持有】网段的行: {solo_rows}  ← 导入必然安全")
print(f"  IP 落在【多单位共享】网段的行: {shared_rows}  ← 后端校验可能不认")
print(f"  IP 不落在任何缓存网段的行: {other_only}")

# ---------- ⑤ init-segments 时新建网段的痕迹 ----------
# _gen_new_segment 生成 10.{a}.{b}.1-10.{a}.{b}.254（a=pool_index%256, b 随机）
# 若查询不过滤，第一个单位创建后，其余单位查询都能看到它 → 疑似共享
print("=" * 70)
sample_unit = next(iter(us))
print(f"抽查单位 [{sample_unit}] 的 {len(us[sample_unit])} 条网段:")
for s in us[sample_unit]:
    tag = "共享" if len(seg_owners[s]) > 1 else "独占"
    print(f"  [{tag}] {s}（{len(seg_owners[s])} 个单位）")
