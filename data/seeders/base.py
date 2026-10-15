# data/base.py
# Seeder 抽象基类：统一"登录 → 逐行写 → 汇总"骨架，子类只实现 login 与 _write_one
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class BaseSeeder(ABC):
    SUCCESS_CODE = 200

    def __init__(self, timeout=(5, 10), stop_on_error: bool = False):
        self.timeout = tuple(timeout)
        self.stop_on_error = stop_on_error
        self.session = None
        self._token = None

    @abstractmethod
    def login(self):
        """子类实现：登录并设置 self.session / self._token"""

    @abstractmethod
    def _write_one(self, payload: dict):
        """子类实现单行写入，返回 (code, msg)"""

    def _ensure_login(self):
        if self._token is None:
            self.login()

    def seed(self, columns, rows):
        """逐行发送，返回 (成功数, 失败明细 [(行号, code, msg), ...])"""
        self._ensure_login()
        total = len(rows)
        success = 0
        failures = []
        logger.info("开始造数，共 %d 行（失败%s）",
                    total, "即停止" if self.stop_on_error else "继续")

        for i, row in enumerate(rows, 1):
            payload = dict(zip(columns, row))
            code, msg = self._write_one(payload)
            if code == self.SUCCESS_CODE:
                success += 1
                logger.info("[%d/%d] 成功", i, total)
            else:
                failures.append((i, code, msg))
                logger.error("[%d/%d] 失败 code=%s msg=%s，请求体=%s",
                             i, total, code, msg, _short(payload))
                if self.stop_on_error:
                    logger.error("已配置 stop_on_error=true，提前终止")
                    break

        self._print_summary(success, failures, total)
        return success, failures

    @staticmethod
    def _print_summary(success, failures, total):
        failed = len(failures)
        print("\n" + "=" * 50)
        print(f"造数完成：总 {total} 行，成功 {success}，失败 {failed}")
        if failures:
            print("失败明细（最多展示 20 条）:")
            for line_no, code, msg in failures[:20]:
                print(f"  第 {line_no} 行  code={code}  msg={msg}")
            if failed > 20:
                print(f"  ...其余 {failed - 20} 条见上方日志")
        print("=" * 50)


def _short(payload, limit: int = 300) -> str:
    text = str(payload)
    return text if len(text) <= limit else text[:limit] + "..."