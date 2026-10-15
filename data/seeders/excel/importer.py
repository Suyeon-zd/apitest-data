# data/importer.py
# 资产 Excel 批量导入执行器：逐文件 multipart 上传 → 全部完成后一次总对账 → manifest 断点续传
#
# 与旧版差异：对账由"每文件一次 COUNT"改为"全部上传完成后一次 COUNT"，
# 避免百万行场景下 100+ 次 COUNT 累积耗时；细粒度行数差异仍可在 manifest 中查看。
#
# 独立性约定：仅依赖 requests/openpyxl + 标准库 logging，不 import core.*
import json
import logging
import datetime as _dt
from pathlib import Path

import openpyxl

from data.infra.auth_kit import login_session
from data.seeders.excel.writer import XLSX_MIME

logger = logging.getLogger(__name__)


class AssetImporter:
    """逐文件调用导入接口批量入库"""

    def __init__(self, base_url, env_config, excel_dir, import_path,
                 import_form, batch_prefix, ck_client, timeout=(5, 120)):
        self.base_url = base_url.rstrip("/")
        self.env = env_config
        self.excel_dir = Path(excel_dir)
        self.import_url = self.base_url + import_path
        self.import_form = dict(import_form)
        self.batch_prefix = batch_prefix
        self.ck = ck_client
        self.timeout = tuple(timeout)
        self.manifest_path = self.excel_dir / "import_manifest.json"

    def run(self) -> dict:
        """执行全部文件导入：逐文件上传 → 全部完成后一次总对账

        返回汇总 {total, success, failed, warned}：
          failed  —— HTTP code != 200 的文件数
          warned  —— HTTP 200 但总对账不符（静默丢行）
        """
        if not self.excel_dir.is_dir():
            raise FileNotFoundError(f"Excel 目录不存在: {self.excel_dir}")
        files = sorted(p.name for p in self.excel_dir.glob("*.xlsx"))
        if not files:
            raise FileNotFoundError(f"目录中没有可导入的 xlsx: {self.excel_dir}")

        manifest = self._load_manifest()
        session, _ = login_session(self.base_url, self.env)

        # 上传前底账基线（用于最后总对账计算增量）
        base_count = self._count_ledger()
        total_expected_new = 0
        summary = {"total": len(files), "success": 0, "failed": 0, "warned": 0}

        # ---------- ① 逐文件上传（不逐个对账） ----------
        for name in files:
            entry = manifest.get(name, {"status": "pending"})
            if entry.get("status") == "success":
                summary["success"] += 1
                logger.info("跳过已成功文件: %s", name)
                continue

            expected_rows = self._excel_rows(self.excel_dir / name)
            code, msg = self._upload_one(session, name)
            entry["rows"] = expected_rows

            if code != 200:
                entry.update(status="failed", msg=msg)
                summary["failed"] += 1
                logger.error("文件 %s 导入失败: code=%s msg=%s", name, code, msg)
            else:
                # 先标记为已上传，等总对账确认是否真的落库
                entry.update(status="uploaded", msg=msg)
                total_expected_new += expected_rows
                logger.info("文件 %s 已上传: %d 行", name, expected_rows)

            manifest[name] = entry
            self._save_manifest(manifest)

        # ---------- ② 全部完成后一次总对账 ----------
        if total_expected_new > 0:
            final_count = self._count_ledger()
            total_delta = final_count - base_count
            if total_delta == total_expected_new:
                for name, entry in manifest.items():
                    if entry.get("status") == "uploaded":
                        entry.update(status="success", delta=entry.get("rows", 0))
                        summary["success"] += 1
                logger.info("总对账通过: 增量 %d == 预期 %d", total_delta, total_expected_new)
            else:
                for name, entry in manifest.items():
                    if entry.get("status") == "uploaded":
                        entry.update(
                            status="warned",
                            delta=total_delta,
                            msg=f"{entry.get('msg', '')}；总对账不符 增量 {total_delta}≠预期 {total_expected_new}",
                        )
                        summary["warned"] += 1
                logger.warning("总对账不符: 增量 %d ≠ 预期 %d（存在静默丢行）",
                               total_delta, total_expected_new)
            self._save_manifest(manifest)

        self._print_summary(summary, self.manifest_path)
        return summary

    def _upload_one(self, session, file_name) -> tuple:
        """上传单个文件，返回 (code, msg)；网络异常不抛出，转 (-1, 文案)"""
        path = self.excel_dir / file_name
        try:
            with open(path, "rb") as f:
                files = {"file": (file_name, f, XLSX_MIME)}
                resp = session.post(self.import_url, data=self.import_form,
                                    files=files, timeout=self.timeout)
            body = resp.json()
        except Exception as e:   # noqa: BLE001
            return -1, f"上传异常: {e}"
        return body.get("code", resp.status_code), body.get("msg", "")

    def _count_ledger(self) -> int:
        sql = (f"SELECT count() AS c FROM ODS.resource_ledger "
               f"WHERE asset_code LIKE '{self.batch_prefix}%'")
        rows = self.ck.query(sql)
        return int(rows[0]["c"]) if rows else 0

    @staticmethod
    def _excel_rows(path) -> int:
        """Excel 实际数据行数：第 1 行表头、第 2 行样例不计，其余为数据"""
        wb = openpyxl.load_workbook(path, read_only=True)
        try:
            ws = wb[wb.sheetnames[0]]
            return max(ws.max_row - 2, 0)
        finally:
            wb.close()

    def _load_manifest(self) -> dict:
        if self.manifest_path.is_file():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("files") or {}
            except (ValueError, OSError):
                logger.warning("manifest 损坏，重新建立: %s", self.manifest_path)
        return {}

    def _save_manifest(self, files: dict):
        payload = {
            "updated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "batch_prefix": self.batch_prefix,
            "files": files,
        }
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _print_summary(summary: dict, manifest_path=None):
        print("\n" + "=" * 50)
        print(f"资产导入完成：文件总 {summary['total']} 个，"
              f"成功 {summary['success']}，行数不符 {summary['warned']}，"
              f"失败 {summary['failed']}")
        if manifest_path:
            print(f"进度文件: {manifest_path}")
        print("=" * 50)