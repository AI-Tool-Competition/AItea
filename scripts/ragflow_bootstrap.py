"""Provision the local integration account and remote embedding/rerank models."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.configuration.paths import InstancePaths  # noqa: E402
from app.configuration.environment import (  # noqa: E402
    environment_content,
    environment_lock,
    write_environment,
)
from urllib.parse import quote

import httpx
from dotenv import dotenv_values, set_key


def register_model(request, runtime, *, provider, instance, kind, api_key, base_url, model):
    """Register a remote provider using the pinned v1.0.0-rc2 instance API."""
    _, providers = request("GET", "/providers")
    if not any(item.get("name", item.get("provider_name")) == provider for item in providers):
        request("PUT", "/providers", {"provider_name": provider})
    path = "/providers/" + quote(provider, safe="") + "/instances"
    _, instances = request("GET", path)
    instances = instances if isinstance(instances, list) else instances.get("instances", [])
    existing = next(
        (item for item in instances if item.get("instance_name", item.get("name")) == instance),
        None,
    )
    payload = {
        "instance_name": instance,
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model_info": [{"model_name": model, "model_type": [kind], "max_tokens": 8192}],
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    saved = runtime / (kind + "-profile.sha256")
    if existing is None:
        request("POST", path, payload)
    elif not saved.exists() or saved.read_text().strip() != fingerprint:
        request("PUT", path + "/" + instance, payload)
    _, registered_models = request("GET", "/models")
    matching = [
        item
        for item in registered_models
        if item.get("name") == model
        and item.get("provider_name") == provider
        and item.get("instance_name") == instance
        and kind in item.get("model_type", [])
    ]
    if len(matching) != 1 or not matching[0].get("model_id"):
        raise RuntimeError(f"未找到唯一的 RAGFlow {kind} 模型实例。")
    saved.write_text(fingerprint + "\n")
    saved.chmod(0o600)
    return matching[0]["model_id"]


def bootstrap(root):
    from ragflow import prepare

    root = Path(root).resolve()
    paths = InstancePaths.resolve(root)
    original = environment_content(paths.env_file)
    # Always refresh the copied provider settings, including after admin edits.
    prepare(root)
    runtime = paths.ragflow_config
    private = runtime / ".env"
    values = dotenv_values(private, interpolate=False)
    embedding = [values.get("AITEA_EMBEDDING_" + field) for field in ("API_KEY", "BASE_URL", "MODEL")]
    rerank = [values.get("AITEA_RERANK_" + field) for field in ("API_KEY", "BASE_URL", "MODEL")]
    if not all(embedding):
        raise RuntimeError("请先配置 EMBEDDING_BASE_URL、EMBEDDING_API_KEY 和 EMBEDDING_MODEL。")
    if any(rerank) and not all(rerank):
        raise RuntimeError("请同时配置 RERANK_BASE_URL、RERANK_API_KEY 和 RERANK_MODEL，或将三项全部清空。")
    base = "http://127.0.0.1:" + values["AITEA_RAGFLOW_API_PORT"]
    token = None

    def request(method, path, payload=None, *, bearer=None, allow_error=False):
        headers = (
            {"Authorization": "Bearer " + bearer} if bearer else ({"Authorization": token} if token else {})
        )
        with httpx.Client(timeout=120, trust_env=False, follow_redirects=False) as client:
            response = client.request(method, base + "/api/v1" + path, json=payload, headers=headers)
        result = response.json()
        if response.status_code != 200 or result.get("code") != 0:
            if allow_error:
                return response, None
            # Responses can include submitted secrets: report only status/code.
            raise RuntimeError(
                f"RAGFlow 初始化 {path} 未成功：HTTP {response.status_code}，code={result.get('code')}。"
            )
        return response, result.get("data")

    # Upstream v1.0 expects a PKCS#1 private key for its password protocol.
    key = runtime / "conf/private.pem"
    if key.read_text().startswith("-----BEGIN PRIVATE KEY-----"):
        converted = runtime / "conf/private-converted.pem"
        subprocess.run(
            ["openssl", "rsa", "-in", str(key), "-traditional", "-out", str(converted)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        converted.chmod(0o600)
        converted.replace(key)
    encrypted = subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-encrypt",
            "-pubin",
            "-inkey",
            str(runtime / "conf/public.pem"),
            "-pkeyopt",
            "rsa_padding_mode:pkcs1",
        ],
        input=values["RAGFLOW_SERVICE_PASSWORD"].encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
    ).stdout
    auth = {
        "email": values["RAGFLOW_SERVICE_EMAIL"],
        "password": base64.b64encode(encrypted).decode(),
    }
    response, data = request("POST", "/auth/login", auth, allow_error=True)
    if data is None:
        response, data = request("POST", "/users", auth | {"nickname": "AItea 资料库"})
    token = response.headers.get("Authorization")
    if not token:
        raise RuntimeError("RAGFlow 未返回登录凭证。")

    provider, instance = "OpenAI-API-Compatible", "aitea_cloud"
    model_id = register_model(
        request,
        runtime,
        provider=provider,
        instance=instance,
        kind="embedding",
        api_key=embedding[0],
        base_url=embedding[1],
        model=embedding[2],
    )
    request(
        "PATCH",
        "/models/default",
        {
            "model_provider": provider,
            "model_instance": instance,
            "model_name": embedding[2],
            "model_type": "embedding",
        },
    )
    changes = {
        "RAGFLOW_BASE_URL": base,
        "RAGFLOW_EMBEDDING_MODEL": model_id,
        "RAGFLOW_CHUNK_CONCURRENCY": "8",
    }
    if all(rerank):
        # In v1.0.0-rc2 OpenAI-API-Compatible inherits an unimplemented rerank
        # method. VLLM implements the existing flat-documents /rerank protocol.
        changes["RAGFLOW_RERANK_MODEL"] = register_model(
            request,
            runtime,
            provider="VLLM",
            instance="aitea_rerank",
            kind="rerank",
            api_key=rerank[0],
            base_url=rerank[1],
            model=rerank[2],
        )
    elif (runtime / "rerank-profile.sha256").exists():
        # Disable only a model managed by this integration; retain manual IDs.
        changes["RAGFLOW_RERANK_MODEL"] = ""
    api_token = values.get("RAGFLOW_API_KEY")
    if api_token:
        _, check = request("GET", "/datasets?page_size=1", bearer=api_token, allow_error=True)
        if check is None:
            api_token = None
    if not api_token:
        _, created = request("POST", "/system/keys", {"name": "AItea backend"})
        api_token = (
            created
            if isinstance(created, str)
            else next(
                (created.get(key) for key in ("token", "api_key", "key") if created.get(key)),
                None,
            )
        )
    if not api_token:
        raise RuntimeError("RAGFlow 未返回服务密钥。")
    set_key(str(private), "RAGFLOW_API_KEY", api_token)
    changes["RAGFLOW_API_KEY"] = api_token
    with environment_lock(paths.env_file):
        if environment_content(paths.env_file) != original:
            raise RuntimeError("同步期间实例配置已变更，请核对最新配置后重新同步。")
        write_environment(paths.env_file, original, changes)
    os.chmod(private, 0o600)
    print("RAGFlow 服务账号、后端密钥和已配置的云端模型已同步。请重启所属后端及采集服务。")
    return 0
