# 临时诊断脚本：检查 sys_dept 重名单位 + 网段表结构
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # data/tools/ 上两级 = 项目根

from data.infra.env_loader import load_dotenv as _load_dotenv, load_env_config as _load_env_config  # noqa: E402
from data.db.client import DBManager                   # noqa: E402

_load_dotenv()
env = _load_env_config("dev")
mgr = DBManager(env.get("databases") or {})
try:
    mysql = mgr.get("mysql")

    # ① sys_dept 中重名的单位（dept_category 非空，与 init-segments 同条件）
    print("=" * 60)
    print("① sys_dept 重名单位（dept_category 非空）：")
    dup = mysql.query(
        "SELECT dept_name, COUNT(*) AS c, GROUP_CONCAT(dept_id) AS ids, "
        "GROUP_CONCAT(DISTINCT com_code) AS com_codes "
        "FROM sys_dept WHERE dept_category IS NOT NULL AND dept_name != '' "
        "GROUP BY dept_name HAVING c > 1 LIMIT 30"
    )
    if not dup:
        print("  无重名单位")
    for r in dup:
        print(f"  {r['dept_name']} | 出现 {r['c']} 次 | dept_id={r['ids']} | com_code={r['com_codes']}")

    # ② init-segments 实际取的前 200 个单位里有没有重名
    print("=" * 60)
    print("② init-segments 的 LIMIT 200 结果中重名的单位：")
    rows = mysql.query(
        "SELECT dept_id, dept_name, dept_category FROM sys_dept "
        "WHERE dept_category IS NOT NULL AND dept_name != '' LIMIT 200"
    )
    from collections import Counter
    cnt = Counter(r["dept_name"] for r in rows)
    dups = {k: v for k, v in cnt.items() if v > 1}
    if dups:
        for name, c in dups.items():
            ids = [r["dept_id"] for r in rows if r["dept_name"] == name]
            print(f"  {name} | {c} 次 | dept_id={ids}")
    else:
        print("  LIMIT 200 内无重名")

    # ③ 找网段管理相关的表
    print("=" * 60)
    print("③ 含 network 的表：")
    tables = mysql.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name LIKE '%network%'"
    )
    for t in tables:
        name = t["table_name"] if "table_name" in t else t["TABLE_NAME"]
        print(f"  表: {name}")
        cols = mysql.query(f"SHOW COLUMNS FROM `{name}`")
        print(f"    列: {[c['Field'] for c in cols]}")
        sample = mysql.query(f"SELECT * FROM `{name}` LIMIT 2")
        for s in sample:
            print(f"    样例: {s}")
finally:
    mgr.close()
