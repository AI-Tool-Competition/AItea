import json

import pytest
from app.contexts.workspace.infrastructure.defaults import DEFAULT_TEMPLATE, load_template


def test_local_configuration_is_read_and_validated(tmp_path):
    path = tmp_path / "workflow.json"
    changed = dict(DEFAULT_TEMPLATE, title="法务配置的流程")
    path.write_text(json.dumps(changed))
    assert load_template(path)["title"] == "法务配置的流程"


def test_invalid_configuration_fails_early(tmp_path):
    path = tmp_path / "workflow.json"
    path.write_text('{"stages": []}')
    with pytest.raises(ValueError, match="流程配置无效"):
        load_template(path)


def test_environment_can_select_a_configuration_file(tmp_path, monkeypatch):
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(dict(DEFAULT_TEMPLATE, title="环境选定的流程")))
    monkeypatch.setenv("TEA_TEMPLATE_FILE", str(path))
    assert load_template()["title"] == "环境选定的流程"
