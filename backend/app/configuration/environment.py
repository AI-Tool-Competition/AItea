"""Load one explicit backend configuration without exporting it to the frontend."""

import fcntl
import hashlib
import os
import tempfile
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values, set_key

from app.configuration.paths import PROJECT_ROOT, InstancePaths

_startup_environment = None
MAX_ENVIRONMENT_BYTES = 64 * 1024


def private_environment(content):
    if len(content) > MAX_ENVIRONMENT_BYTES:
        raise ValueError("实例环境配置不能超过 64 KiB。")
    values = {
        key: value
        for key, value in dotenv_values(stream=StringIO(content.decode("utf-8")), interpolate=False).items()
        if value is not None
    }
    if any(values.get(key) for key in ("TEA_ENV_FILE", "TEA_DATA_DIR", "TEA_CONFIG_DIR")):
        raise ValueError("TEA_ENV_FILE、TEA_DATA_DIR、TEA_CONFIG_DIR 请在启动环境设置，不写入配置文件。")
    return values


def environment_content(path):
    try:
        with Path(path).open("rb") as source:
            content = source.read(MAX_ENVIRONMENT_BYTES + 1)
    except FileNotFoundError:
        content = b""
    private_environment(content)
    return content


def environment_revision(content):
    return hashlib.sha256(content).hexdigest()


def startup_environment():
    """Explicit launcher values retain precedence over editable file values."""
    return dict(_startup_environment or {})


@contextmanager
def environment_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_environment(path, content, changes):
    """Caller holds environment_lock and has checked the content revision."""
    descriptor, temporary = tempfile.mkstemp(prefix=".application-", suffix=".env", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
        for key, value in changes.items():
            set_key(temporary, key, value, quote_mode="always")
        updated = environment_content(temporary)
        os.chmod(temporary, 0o600)
        with open(temporary, "rb") as target:
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return updated
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_environment(project=PROJECT_ROOT, *, environ=None):
    env = os.environ if environ is None else environ
    paths = InstancePaths.resolve(project, environ=env)
    if env.get("TEA_ENV_FILE") and not paths.env_file.is_file():
        raise ValueError("TEA_ENV_FILE 指定的配置文件不存在。")
    values = private_environment(environment_content(paths.env_file))
    # Location selectors belong to the launcher, so loading a file cannot
    # redirect the instance or silently switch to a second set of secrets.
    return values | dict(env)


def load_environment(project=PROJECT_ROOT):
    global _startup_environment
    if _startup_environment is None:
        _startup_environment = dict(os.environ)
    values = read_environment(project)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return InstancePaths.resolve(project)
