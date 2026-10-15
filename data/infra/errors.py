# data/errors.py
# 统一异常体系：data 层错误从 DataError 派生，code 用于 cli 进程退出码


class DataError(Exception):
    """data 层异常基类"""
    code = 1

    def __init__(self, message, code=None):
        super().__init__(message)
        if code is not None:
            self.code = code


class ConfigError(DataError):
    """配置/表定义错误"""
    code = 1


class ValidationError(DataError):
    """YAML 校验失败"""
    code = 1


class ConnectionError(DataError):  # noqa: A001 - 有意覆盖内建名，语义一致
    """数据库连接失败"""
    code = 1


class GenerationError(DataError):
    """数据生成错误"""
    code = 1


class LoginError(DataError):
    """登录失败"""
    code = 1


class PartialFailure(DataError):
    """部分行/部分文件失败（非零退出码，便于 CI 感知）"""
    code = 2