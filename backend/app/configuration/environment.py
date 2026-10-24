"""Load one explicit backend configuration without exporting it to the frontend."""

import os
from dotenv import dotenv_values

from app.configuration.paths import PROJECT_ROOT, InstancePaths


def read_environment(project=PROJECT_ROOT, *, environ=None):
    env = os.environ if environ is None else environ
    paths = InstancePaths.resolve(project, environ=env)
    if env.get("TEA_ENV_FILE") and not paths.env_file.is_file():
        raise ValueError("TEA_ENV_FILE 指定的配置文件不存在。")
    if paths.env_file.exists() and paths.env_file.stat().st_size > 64 * 1024:
        raise ValueError("实例环境配置不能超过 64 KiB。")
    values = {
        key: value
        for key, value in dotenv_values(paths.env_file, interpolate=False).items()
        if value is not None
    }
    # Location selectors belong to the launcher, so loading a file cannot
    # redirect the instance or silently switch to a second set of secrets.
    if any(values.get(key) for key in ("TEA_ENV_FILE", "TEA_DATA_DIR", "TEA_CONFIG_DIR")):
        raise ValueError("TEA_ENV_FILE、TEA_DATA_DIR、TEA_CONFIG_DIR 请在启动环境设置，不写入配置文件。")
    return values | dict(env)


def load_environment(project=PROJECT_ROOT):
    values = read_environment(project)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return InstancePaths.resolve(project)
