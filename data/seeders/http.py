# data/http.py
# 接口造数输出适配器（target: api）：登录拿 Token → 按行把数据 POST 到业务接口
#
# 独立性约定：仅依赖 requests + 标准库 logging，不 import core.*
import logging

import requests

from data.seeders.base import BaseSeeder
from data.infra.errors import ConfigError

logger = logging.getLogger(__name__)


class HttpSeeder(BaseSeeder):
    """逐行调用业务接口批量造数

    :param base_url: 环境基础地址，如 http://10.148.242.205/dev-api
    :param env_config: 环境配置（config/<env>.yaml 展开后）
    :param api_spec: 表定义中的 api 段：{method: POST, path: /system/user}
    :param timeout: requests 超时（连接, 读取）秒
    :param stop_on_error: True=首条失败即停止；False=失败继续，最后汇总
    """

    def __init__(self, base_url: str, env_config: dict, api_spec: dict,
                 timeout=(5, 10), stop_on_error: bool = False):
        super().__init__(timeout=timeout, stop_on_error=stop_on_error)
        if not base_url:
            raise ConfigError("接口造数缺少 base_url（请检查 config/<env>.yaml）")
        if not api_spec.get("path"):
            raise ConfigError("表定义 api 段缺少 path（如 /system/user）")

        self.base_url = base_url.rstrip("/")
        self.env = env_config or {}
        self.method = str(api_spec.get("method", "POST")).upper()
        self.path = api_spec["path"]
        self.url = self.base_url + self.path

    # ---------- 实现 BaseSeeder 抽象方法 ----------

    def login(self):
        """登录获取 access_token（共享 data.auth_kit.login_session）"""
        from data.infra.auth_kit import login_session
        self.session, token = login_session(self.base_url, self.env, timeout=self.timeout)
        self._token = token
        logger.info("开始接口造数: %s %s", self.method, self.url)
        return token

    def _write_one(self, payload: dict):
        """发送单行请求，返回 (code, msg)；网络/解析异常转为 (-1, 文案)，不抛出"""
        try:
            resp = self.session.request(
                self.method, self.url, json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            return -1, f"请求异常: {e}"

        try:
            body = resp.json()
        except ValueError:
            return resp.status_code, f"响应不是 JSON: {resp.text[:200]}"
        return body.get("code", resp.status_code), body.get("msg", "")