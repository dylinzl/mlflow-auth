import configparser
from pathlib import Path
from typing import NamedTuple

from mlflow.environment_variables import MLFLOW_AUTH_CONFIG_PATH


class AuthConfig(NamedTuple):
    default_permission: str
    database_uri: str
    admin_username: str
    admin_password: str
    authorization_function: str
    session_config: dict


def _get_auth_config_path() -> str:
    return (
        MLFLOW_AUTH_CONFIG_PATH.get() or Path(__file__).parent.joinpath("basic_auth.ini").resolve()
    )


def read_auth_config() -> AuthConfig:
    config_path = _get_auth_config_path()
    config = configparser.ConfigParser()
    config.read(config_path, encoding='utf-8')
    
    # 读取会话配置
    session_config = {}
    if config.has_section("session"):
        for key, value in config.items("session"):
            # 处理布尔值
            if value.lower() in ("true", "false"):
                session_config[key.upper()] = value.lower() == "true"
            # 处理数字值
            elif value.isdigit():
                session_config[key.upper()] = int(value)
            else:
                session_config[key.upper()] = value
    
    return AuthConfig(
        default_permission=config["mlflow"]["default_permission"],
        database_uri=config["mlflow"]["database_uri"],
        admin_username=config["mlflow"]["admin_username"],
        admin_password=config["mlflow"]["admin_password"],
        authorization_function=config["mlflow"].get(
            "authorization_function", "mlflow.server.auth:authenticate_request_basic_auth"
        ),
        session_config=session_config,
    )
