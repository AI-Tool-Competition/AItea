#!/usr/bin/env python3
"""Check real production images with an owned, disposable library and local model.

Works with the calling user's remote Docker as well. Never mounts project data,
loads a private instance environment, or contacts a paid model.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path

from production import ROOT, compose_command, docker_environment, settings


INITIALIZE = r"""
import json,os,shutil,sys,socket
from pathlib import Path
from app.bootstrap import Service
data=Path(sys.argv[1])
uid=int(sys.argv[2]);gid=int(sys.argv[3])
data.mkdir(parents=True,exist_ok=True)
(data/'config').mkdir(exist_ok=True)
shutil.copytree('/app/config',data/'config/catalogs')
sockets=[]
ports=[]
for _ in range(4):
    s=socket.socket();s.bind(('127.0.0.1',0));sockets.append(s);ports.append(s.getsockname()[1])
private=data/'config/application.env'
private.write_text("AGENT_MODEL=smoke-local\nAGENT_API_KEY=smoke-only\nAGENT_BASE_URL=http://127.0.0.1:"+str(ports[3])+"/v1\nAGENT_REASONING_EFFORT=high\nAGENT_BUDGET_MODEL=5\nAGENT_BUDGET_TOTAL=15\nAGENT_BUDGET_EVIDENCE=3\nAGENT_BUDGET_ANSWER_RESERVE=3\nAGENT_STREAM_IDLE_TIMEOUT=5\nVISION_OCR_ENABLED=false\n")
os.environ['TEA_ENV_FILE']=str(private)
Service(data)
for path in [data,*data.rglob('*')]:
    if not path.is_symlink():os.chown(path,uid,gid)
private.chmod(0o600)
print(json.dumps({'ports':ports}))
"""

PROBE = r"""
import http.cookiejar,json,re,sys,time,urllib.error,urllib.parse,urllib.request
port=int(sys.argv[1]);base='http://127.0.0.1:'+str(port)+'/aitea'
jar=http.cookiejar.CookieJar()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(jar))
def request(path,payload=None):
    data=json.dumps(payload).encode() if payload is not None else None
    return opener.open(urllib.request.Request(base+path,data=data,headers={'Content-Type':'application/json','Origin':base.removesuffix('/aitea')}),timeout=30)
def load(path,payload=None):
    with request(path,payload) as response:return json.load(response)
health=load('/api/tea/health');assert health['status']=='ok'
with opener.open(urllib.request.Request(base+'/api/tea/health',headers={'Host':'aitea.example.invalid','X-Forwarded-Proto':'https'}),timeout=30) as response:
    assert response.status==200
with request('') as response:
    assert urllib.parse.urlsplit(response.url).path=='/aitea/tea',response.url
    html=response.read().decode()
assets=re.findall(r'<script[^>]+src="([^"]+)"',html)
assert assets and all(path.startswith('/aitea/_next/') for path in assets),assets[:3]
with opener.open('http://127.0.0.1:'+str(port)+assets[0],timeout=30) as response:
    assert response.status==200 and 'immutable' in response.headers.get('Cache-Control','')
user=load('/api/tea/auth/demo',{})
assert load('/api/tea/auth/me')['id']==user['id']
assert all(cookie.path=='/aitea' for cookie in jar)
boot=load('/api/tea/bootstrap')
case=load('/api/tea/cases/'+boot['cases'][0]['id'])
stage=case['stages'][0]['id']
conversation=load('/api/tea/cases/'+case['id']+'/stages/'+stage+'/guide-conversation',{})
payload={'stage_id':stage,'conversation_id':conversation['id'],'content':'这是部署连接测试，请简短确认。','client_message_id':'docker-smoke-once'}
started=time.monotonic();deltas=[];events=[];event='';done=None
with request('/api/tea/cases/'+case['id']+'/messages/stream',payload) as response:
    assert response.headers['Content-Type'].startswith('text/event-stream')
    assert response.headers.get('X-Accel-Buffering')=='no'
    assert 'no-transform' in response.headers.get('Cache-Control','')
    for raw in response:
        line=raw.decode().strip()
        if line.startswith('event:'):event=line[6:].strip()
        elif line.startswith('data:'):
            value=json.loads(line[5:]);events.append(event)
            if event=='delta':deltas.append((time.monotonic()-started,value.get('text','')))
            if event=='done':done=value;break
assert done is not None,events
assert len(deltas)>=2,deltas
assert deltas[-1][0]-deltas[0][0]>0.25,{'deltas':deltas,'events':events}
assert '容器连接正常' in ''.join(text for _,text in deltas),done
with request('/api/tea/auth/logout',{}) as response:assert response.status==204
try:load('/api/tea/auth/me')
except urllib.error.HTTPError as error:assert error.code==401
else:raise AssertionError('session survived logout')
print(json.dumps({'status':'passed','prefix':'/aitea','static_asset':True,'cookie_scope':True,'login_logout':True,'stream_deltas':len(deltas),'first_delta_seconds':round(deltas[0][0],3),'last_delta_seconds':round(deltas[-1][0],3),'duration_seconds':round(time.monotonic()-started,3)}))
"""


def execute(command, env, **kwargs):
    return subprocess.run(command, env=env, check=True, **kwargs)


def cleanup(state, env):
    command = state["compose"]
    subprocess.run(command + ["down", "--timeout", "15"], env=env, check=False)
    subprocess.run(["docker", "rm", "-f", state["provider"]], env=env, check=False, capture_output=True)
    subprocess.run(["docker", "volume", "rm", state["volume"]], env=env, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--engine", choices=("standard", "gvisor"), default="standard")
    parser.add_argument("--keep", action="store_true", help="保留本次隔离实例供浏览器检查；随后显式清理")
    parser.add_argument("--cleanup", type=Path, help="清理本脚本生成的 state.json")
    args = parser.parse_args()
    env = docker_environment()
    if args.cleanup:
        state = json.loads(args.cleanup.read_text())
        if not re.fullmatch(r"[0-9a-f]{12}", state["identity"]):
            raise ValueError("隔离实例标识无效。")
        expected = "aitea-smoke-" + state["identity"]
        if state["volume"] != expected + "-data" or state["provider"] != expected + "-model":
            raise ValueError("拒绝清理不属于本脚本的资源。")
        config = args.cleanup.resolve().parent / "deploy.env"
        values = settings(config)
        if values.get("TEA_COMPOSE_PROJECT") != expected:
            raise ValueError("隔离实例配置归属不符。")
        state["compose"] = compose_command(values, config)
        cleanup(state, env)
        return 0
    if not args.tag or not re.fullmatch(r"[A-Za-z0-9_.-]+", args.tag):
        parser.error("运行隔离检查必须提供有效的 --tag 镜像标签。")
    identity = secrets.token_hex(6)
    name = "aitea-smoke-" + identity
    runtime = ROOT / ".dev/production" / name
    runtime.mkdir(parents=True, mode=0o700)
    image = "aitea-api:" + args.tag
    uid = "0" if args.engine == "gvisor" else "10001"
    state = {"identity": identity, "volume": name + "-data", "provider": name + "-model"}
    success = False
    try:
        execute(["docker", "volume", "create", "--label", "aitea.smoke=" + identity, state["volume"]], env)
        volume = subprocess.check_output(
            ["docker", "volume", "inspect", state["volume"], "--format", "{{.Mountpoint}}"],
            env=env,
            text=True,
        ).strip()
        initialized = execute(
            [
                "docker",
                "run",
                "--rm",
                "--no-healthcheck",
                "--network",
                "host",
                "--user",
                "0:0",
                "--mount",
                "type=volume,source=" + state["volume"] + ",target=" + volume,
                "--entrypoint",
                "python",
                image,
                "-c",
                INITIALIZE,
                volume,
                uid,
                uid,
            ],
            env,
            capture_output=True,
            text=True,
        )
        api, web, gateway, model = json.loads(initialized.stdout.strip().splitlines()[-1])["ports"]
        provider = (ROOT / "backend/tests/fixtures/streaming_provider.py").read_text()
        execute(
            [
                "docker",
                "run",
                "-d",
                "--no-healthcheck",
                "--name",
                state["provider"],
                "--label",
                "aitea.smoke=" + identity,
                "--network",
                "host",
                "--user",
                "0:0" if args.engine == "gvisor" else "10001:10001",
                "--read-only",
                "--cap-drop=ALL",
                "--entrypoint",
                "python",
                image,
                "-u",
                "-c",
                provider,
                str(model),
            ],
            env,
        )
        values = {
            "TEA_INSTANCE_DATA": volume,
            "TEA_INSTANCE_CONFIG": volume + "/config/catalogs",
            "TEA_PUBLIC_ORIGIN": "http://127.0.0.1:" + str(gateway),
            "TEA_BASE_PATH": "/aitea",
            "TEA_UID": uid,
            "TEA_GID": uid,
            "TEA_API_PORT": str(api),
            "TEA_WEB_PORT": str(web),
            "TEA_GATEWAY_PORT": str(gateway),
            "TEA_COOKIE_SECURE": "false",
            "TEA_IMAGE_TAG": args.tag,
            "TEA_DOCKER_NETWORK": "host",
            "TEA_DOCKER_ENGINE": args.engine,
            "TEA_ALLOW_DEMO": "true",
            "TEA_COMPOSE_PROJECT": name,
        }
        config = runtime / "deploy.env"
        config.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        state["compose"] = compose_command(values, config)
        state["gateway_port"] = gateway
        (runtime / "state.json").write_text(json.dumps(state, indent=2) + "\n")
        execute(state["compose"] + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "240"], env)
        result = execute(
            [
                "docker",
                "run",
                "--rm",
                "--no-healthcheck",
                "--network",
                "host",
                "--read-only",
                "--cap-drop=ALL",
                "--entrypoint",
                "python",
                image,
                "-c",
                PROBE,
                str(gateway),
            ],
            env,
            capture_output=True,
            text=True,
        )
        print(result.stdout)
        (runtime / "result.json").write_text(result.stdout)
        success = True
        print(json.dumps({"state": str(runtime / "state.json"), "gateway_port": gateway, "kept": args.keep}))
        return 0
    except subprocess.CalledProcessError as error:
        if error.stderr:
            print(error.stderr, file=sys.stderr)
        if "compose" in state:
            subprocess.run(state["compose"] + ["logs", "--tail", "40"], env=env, check=False)
        return 1
    finally:
        if not (success and args.keep):
            if "compose" in state:
                cleanup(state, env)
            else:
                subprocess.run(["docker", "rm", "-f", state["provider"]], env=env, capture_output=True)
                subprocess.run(["docker", "volume", "rm", state["volume"]], env=env, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
