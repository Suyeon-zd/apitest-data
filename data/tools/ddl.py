# data/tools/ddl.py - 将 data/ddl_export/ 的 DDL 快照批量应用到目标 ClickHouse
# 用法（项目根目录执行）：python -m data.tools.ddl
# 密码从环境变量 CK_PASSWORD 读取（.env 或系统环境变量注入），不再明文入库
import glob
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from clickhouse_driver import Client

# ================= 配置区域 =================
# 目标 ClickHouse 环境的连接信息
NEW_CK_CONFIG = {
    'host': '10.148.242.214',  # 请替换为新环境的IP
    'port': 9000,
    'user': 'default',
    'password': os.environ.get('CK_PASSWORD', ''),  # 从 .env / 环境变量注入
    'database': 'default'  # 默认数据库，建表时会切换
}

# DDL 快照目录（data/ddl_export/，按脚本位置定位，任意 cwd 运行均正确）
IMPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'ddl_export')

# 最大并发线程数，避免瞬间建立过多连接压垮新环境
MAX_WORKERS = 10


# ===========================================

def create_table_from_file(file_path):
    """读取单个txt文件中的DDL并在新环境执行"""
    db_name = os.path.basename(os.path.dirname(file_path))
    table_name = os.path.basename(file_path)

    with open(file_path, 'r', encoding='utf-8') as f:
        ddl_statement = f.read()

    # 去除首尾空白字符（防止有隐藏的回车换行）
    ddl_statement = ddl_statement.strip()
    if not ddl_statement:
        return f"[跳过] 文件 {table_name} 内容为空"

    # 在新环境执行建表语句
    new_client = Client(**NEW_CK_CONFIG)
    try:
        new_client.execute(ddl_statement)
        return f"[成功] {db_name}.{table_name}"
    except Exception as e:
        return f"[失败] {db_name}.{table_name} - 错误信息: {str(e)[:100]}..."
    finally:
        new_client.disconnect()


def import_clickhouse_ddl():
    if not os.path.exists(IMPORT_DIR):
        print(f"错误：找不到导入目录 '{IMPORT_DIR}'，请检查路径。")
        return

    # 1. 获取所有 .txt 文件的完整路径
    all_files = glob.glob(os.path.join(IMPORT_DIR, "**", "*.txt"), recursive=True)
    if not all_files:
        print(f"警告：在 '{IMPORT_DIR}' 及其子目录中未找到任何 .txt 文件。")
        return

    print(f"开始导入，共发现 {len(all_files)} 个表结构文件...")

    # 2. 使用多线程并发执行建表
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(create_table_from_file, file): file for file in all_files}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(result)

    # 3. 输出导入统计信息
    success_count = sum(1 for r in results if "[成功]" in r)
    fail_count = len(results) - success_count

    print(f"\n================= 导入完成 =================")
    print(f"总计: {len(results)} | 成功: {success_count} | 失败: {fail_count}")
    print(f"==========================================")


if __name__ == '__main__':
    import_clickhouse_ddl()