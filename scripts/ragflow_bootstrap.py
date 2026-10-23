"""Provision the local AItea integration account and cloud embedding provider."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import dotenv_values, set_key


def bootstrap(root):
    root = Path(root).resolve()
    runtime = root / ".dev/ragflow"
    private = runtime / ".env"
    values = dotenv_values(private)
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
    api_key = values.get("AITEA_EMBEDDING_API_KEY")
    embedding_base = values.get("AITEA_EMBEDDING_BASE_URL")
    model = values.get("AITEA_EMBEDDING_MODEL")
    if not all((api_key, embedding_base, model)):
        raise RuntimeError("请先配置原有的 EMBEDDING_BASE_URL、EMBEDDING_API_KEY 和 EMBEDDING_MODEL。")
    models = [{"model_name": model, "model_type": ["embedding"], "max_tokens": 8192}]
    payload = {
        "instance_name": instance,
        "api_key": api_key,
        "base_url": embedding_base.rstrip("/"),
        "model_info": models,
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    saved = runtime / "embedding-profile.sha256"
    if existing is None:
        request("POST", path, payload)
    elif not saved.exists() or saved.read_text().strip() != fingerprint:
        request("PUT", path + "/" + instance, payload)
    saved.write_text(fingerprint + "\n")
    saved.chmod(0o600)
    request(
        "PATCH",
        "/models/default",
        {
            "model_provider": provider,
            "model_instance": instance,
            "model_name": model,
            "model_type": "embedding",
        },
    )
    _, registered_models = request("GET", "/models")
    matching_models = [
        item
        for item in registered_models
        if item.get("name") == model
        and item.get("provider_name") == provider
        and item.get("instance_name") == instance
        and "embedding" in item.get("model_type", [])
    ]
    if len(matching_models) != 1 or not matching_models[0].get("model_id"):
        raise RuntimeError("未找到唯一的 RAGFlow embedding 模型实例。")
    model_id = matching_models[0]["model_id"]
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
    application = root / ".env"
    for key, value in {
        "RAGFLOW_BASE_URL": base,
        "RAGFLOW_API_KEY": api_token,
        "RAGFLOW_EMBEDDING_MODEL": model_id,
        "RAGFLOW_CHUNK_CONCURRENCY": "8",
    }.items():
        set_key(str(application), key, value)
    os.chmod(private, 0o600)
    os.chmod(application, 0o600)
    print("RAGFlow 服务账号、后端密钥和云端 embedding 已接通。密钥仅保存在后端私有配置。")
    return 0
