# data.tools 包：场景②（批量造数）的数据侧辅助工具
# 入口统一用 python -m 运行（项目根目录执行）：
#   python -m data.tools.diag_dept        # 诊断 sys_dept 重名单位与网段表结构
#   python -m data.tools.diag_overlap     # 诊断 segments_cache 网段重叠度
#   python -m data.tools.verify_ips       # 校验 Excel 中 IP 是否落在单位网段内
#   python -m data.tools.check_ck         # CK 连通性与库表快速检查
#   python -m data.tools.sync_ck_schema   # 导出 CK 全库 DDL 到 data/ddl_export/
#   python -m data.tools.ddl              # 将 data/ddl_export/ 的 DDL 批量应用到目标 CK
# 仅依赖 data/ 自身（保持 data/ 可整体拷贝的独立性约束）。
