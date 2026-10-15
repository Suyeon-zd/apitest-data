import sys

from data.infra.env_loader import load_dotenv, load_env_config
from data.db.client import DBManager


def main():
    load_dotenv()                                    # ← 关键：加载 .env

    env_cfg = load_env_config("dev")
    mgr = DBManager(env_cfg.get("databases") or {})

    if not mgr.available:
        print("[ERROR] config/<env>.yaml 中没有配置任何数据库（databases 段为空）")
        sys.exit(1)

    # DBManager 为惰性连接：这里首次 get 才真正建连，连不上会抛 ValueError
    try:
        ck = mgr.get("ck")
    except ValueError as e:
        print(f"[ERROR] CK 连接失败: {e}")
        sys.exit(1)

    print("=" * 50)
    print("1. 连的哪个实例：", ck.query("SELECT hostName() AS h"))
    print("2. 当前默认库：  ", ck.query("SELECT currentDatabase() AS db"))
    print("3. 表总数：      ", ck.query("SELECT count(*) AS c FROM ADS.big_sec_event"))
    print("4. 最近 3 行：")
    for row in ck.query("SELECT create_time, event_type FROM ADS.big_sec_event ORDER BY create_time DESC LIMIT 3"):
        print("   ", row)
    print("=" * 50)

    mgr.close()


if __name__ == "__main__":
    main()