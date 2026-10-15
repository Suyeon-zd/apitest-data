# data/infra/auth_kit.py
# data 层共享登录能力：按环境配置登录取 access_token，返回带鉴权头的 requests.Session
import logging

import requests

from data.infra.errors import LoginError

logger = logging.getLogger(__name__)


def login_session(base_url: str, env_config: dict, timeout=(5, 10)):
    """登录并返回 (requests.Session, access_token)

    :param env_config: config/<env>.yaml 展开后的配置，需含 clientid / encrypt-key / payload
    """
    if not base_url:
        raise LoginError("登录缺少 base_url（请检查 config/<env>.yaml）")

    session = requests.Session()
    base_url = base_url.rstrip("/")
    headers = {
        "clientid": env_config["clientid"],
        "encrypt-key": env_config["encrypt-key"],
        "Content-Type": env_config.get("Content-Type", "application/x-www-form-urlencoded"),
    }
    login_path = env_config.get("login_path", "/auth/login")
    url = base_url + login_path
    logger.info("登录获取 Token: POST %s", url)

    try:
        resp = session.post(url, data=env_config.get("payload"),
                            headers=headers, timeout=tuple(timeout))
        body = resp.json()
    except requests.RequestException as e:
        raise LoginError(f"登录请求异常: {e}") from e
    except ValueError:
        raise LoginError(
            f"登录失败: 响应不是 JSON (status={resp.status_code}, body={resp.text[:200]})"
        )

    token = body.get("access_token")
    if not token and isinstance(body.get("data"), dict):
        token = body["data"].get("access_token")
    if not token:
        raise LoginError(f"登录失败: 响应中无 access_token，body={str(body)[:200]}")

    session.headers.update({
        "Authorization": f"Bearer {token}",
        "clientid": env_config["clientid"],
    })
    logger.info("登录成功，Token 已缓存")
    return session, token