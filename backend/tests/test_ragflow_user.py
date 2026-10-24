import sys
import tomllib

from app.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
sys.path.insert(0, str(ROOT / "scripts"))

from ragflow_user import infinity_runtime_config  # noqa: E402


def test_user_runtime_uses_bounded_infinity_file_logs():
    path = ROOT / "deploy/ragflow/infinity.toml"
    config = tomllib.loads(
        infinity_runtime_config(
            path,
            {"postgres_port": 15432, "http_port": 23820, "client_port": 23817},
        )
    )

    assert config["network"]["server_address"] == "127.0.0.1"
    assert config["network"]["postgres_port"] == 15432
    assert config["network"]["http_port"] == 23820
    assert config["network"]["client_port"] == 23817
    assert config["log"]["log_to_stdout"] is False
    assert config["log"]["log_file_max_size"] == "100MB"
    assert config["log"]["log_file_rotate_count"] == 10
