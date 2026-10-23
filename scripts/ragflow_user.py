"""Run the pinned RAGFlow images in single-user namespaces, without sudo.

Apptainer is installed under the user's home. Each checkout has private
configuration, explicit loopback ports, durable data and verified process IDs.
The root ID inside a namespace maps only to the invoking host user.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

IMAGES = {
    "mysql": ("mysql-8.0.40", "mysql:8.0.40"),
    "minio": ("minio-2026-08-06", "pgsty/silo:RELEASE.2026-08-06T00-00-00Z"),
    "kvrocks": ("kvrocks-2.16.0", "apache/kvrocks:2.16.0"),
    "nats": ("nats-2.14.2", "nats:2.14.2"),
    "infinity": ("infinity-0.7.4", "infiniflow/infinity:v0.7.4-x64-v3"),
    "ragflow": ("ragflow-v1.0.0-rc2", "infiniflow/ragflow:v1.0.0-rc2"),
}


def process_start(pid):
    try:
        # comm may contain spaces or parentheses; remaining fields start at 3.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


class UserRuntime:
    def __init__(self, root, source):
        self.root, self.source = Path(root).resolve(), Path(source).resolve()
        self.runtime = self.root / ".dev/ragflow"
        self.data = self.root / "data/ragflow"
        self.tools = Path(os.getenv("AITEA_RAGFLOW_TOOLS", str(Path.home() / ".local/opt/aitea"))).resolve()
        self.apptainer = self.tools / "apptainer-1.5.4/bin/apptainer"
        self.values = {k: v or "" for k, v in dotenv_values(self.runtime / ".env").items()}
        self.app = dotenv_values(self.root / ".env")
        api = int(self.values.get("AITEA_RAGFLOW_API_PORT", "19380"))
        self.ports = {
            "ragflow": api,
            "web": int(self.values.get("AITEA_RAGFLOW_WEB_PORT", "18080")),
            "mysql": api + 2,
            "minio": api + 3,
            "kvrocks": api + 4,
            "nats": api + 5,
            "infinity_http": api + 6,
            "infinity": api + 7,
            "infinity_pg": api + 8,
        }
        if len(set(self.ports.values())) != len(self.ports) or any(
            not 1024 <= p <= 65535 for p in self.ports.values()
        ):
            raise ValueError("RAGFlow 端口需为不同的 1024 至 65535 端口。")
        self.state_file = self.runtime / "processes.json"
        self.state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.env = dict(
            os.environ,
            APPTAINER_CACHEDIR=str(self.tools / "cache"),
            APPTAINER_TMPDIR=str(self.tools / "tmp"),
            APPTAINER_CONFIGDIR=str(self.runtime / "apptainer"),
        )

    def _image(self, service):
        return self.tools / "images" / (IMAGES[service][0] + ".sif")

    def alive(self, service):
        saved = self.state.get(service, {})
        return bool(saved.get("pid") and process_start(saved["pid"]) == saved.get("start"))

    def _save(self):
        self.state_file.write_text(json.dumps(self.state, indent=2) + "\n")
        self.state_file.chmod(0o600)

    def _command(self, service, command, *, binds=(), pwd="/", root_user=False):
        if self.alive("network"):
            prefix = self._namespace()
            if not root_user:
                prefix += [
                    "unshare",
                    "--user",
                    f"--map-user={os.getuid()}",
                    f"--map-group={os.getgid()}",
                ]
        else:
            prefix = ["unshare", "-Ur"] if root_user else []
        args = prefix + [
            str(self.apptainer),
            "exec",
            "--userns",
            "--contain",
            "--cleanenv",
            "--no-home",
            "--no-mount",
            "cwd",
            "--pwd",
            pwd,
            "--writable-tmpfs",
        ]
        if self.alive("network"):
            args += ["--bind", str(self.runtime / "resolv.conf") + ":/etc/resolv.conf"]
        for source, target in binds:
            args += ["--bind", str(source) + ":" + target]
        return [*args, str(self._image(service)), *command]

    def _spawn(
        self,
        name,
        command,
        *,
        image=None,
        binds=(),
        pwd="/",
        root_user=False,
        environment=None,
    ):
        if self.alive(name):
            return
        image = image or name
        env = dict(self.env)
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            env["APPTAINERENV_" + key] = ""
        env["APPTAINERENV_NO_PROXY"] = "*"
        env["APPTAINERENV_no_proxy"] = "*"
        for key, value in (environment or {}).items():
            env["APPTAINERENV_" + key] = str(value)
        env["APPTAINERENV_AITEA_RAGFLOW_INSTANCE"] = hashlib.sha256(str(self.root).encode()).hexdigest()
        self._spawn_host(
            name,
            self._command(image, command, binds=binds, pwd=pwd, root_user=root_user),
            env,
        )

    def _spawn_host(self, name, command, env):
        log = self.runtime / (name + ".log")
        descriptor = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "ab") as output:
            process = subprocess.Popen(
                command,
                cwd=self.runtime,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.state[name] = {
            "pid": process.pid,
            "start": process_start(process.pid),
            "log": str(log),
        }
        self._save()

    def _namespace(self):
        return [
            "nsenter",
            "-t",
            str(self.state["network"]["pid"]),
            "-U",
            "-n",
            "--preserve-credentials",
        ]

    def _network(self):
        slirp = self.tools / "slirp4netns-1.3.6"
        if not slirp.is_file():
            raise RuntimeError("用户目录中缺少 slirp4netns。请先执行用户态安装。")
        if not self.alive("network"):
            self._spawn_host("network", ["unshare", "-Urn", "sleep", "infinity"], self.env)
            time.sleep(0.1)
        socket_path = self.runtime / "network.sock"
        if not self.alive("nat"):
            socket_path.unlink(missing_ok=True)
            env = dict(
                self.env,
                LD_LIBRARY_PATH=str(self.tools / "bootstrap/usr/lib/x86_64-linux-gnu"),
            )
            self._spawn_host(
                "nat",
                [
                    str(slirp),
                    "--configure",
                    "--mtu=65520",
                    "--disable-host-loopback",
                    "--api-socket",
                    str(socket_path),
                    str(self.state["network"]["pid"]),
                    "tap0",
                ],
                env,
            )
            self._wait("nat", socket_path.exists, timeout=15)
        (self.runtime / "resolv.conf").write_text("nameserver 10.0.2.3\n")

        def request(payload):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(3)
                connection.connect(str(socket_path))
                connection.sendall(json.dumps(payload).encode() + b"\n")
                connection.shutdown(socket.SHUT_WR)
                raw = connection.recv(65536)
            result = json.loads(raw)
            if "error" in result:
                raise RuntimeError("用户态端口转发配置失败。")
            return result.get("return", result)

        current = request({"execute": "list_hostfwd"})
        ports = {rule["host_port"] for rule in current.get("entries", [])}
        for name in ("ragflow", "web"):
            port = self.ports[name]
            if port not in ports:
                request(
                    {
                        "execute": "add_hostfwd",
                        "arguments": {
                            "proto": "tcp",
                            "host_addr": "127.0.0.1",
                            "host_port": port,
                            "guest_addr": "10.0.2.100",
                            "guest_port": port,
                        },
                    }
                )

    def _probe_namespace(self, port, path=None):
        if path is None:
            code = (
                "import socket,sys;socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=2).close()"
            )
            args = [str(port)]
        else:
            code = "import urllib.request,sys;opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));r=opener.open('http://127.0.0.1:'+sys.argv[1]+sys.argv[2],timeout=2);assert r.status==200"
            args = [str(port), path]
        return (
            subprocess.run(
                self._namespace() + [sys.executable, "-c", code, *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            ).returncode
            == 0
        )

    def _wait(self, name, probe, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive(name):
                raise RuntimeError(f"{name} 启动退出，日志：{self.runtime / (name + '.log')}")
            try:
                if probe():
                    print(name + " 已就绪", flush=True)
                    return
            except (OSError, httpx.HTTPError, ValueError):
                pass
            time.sleep(1)
        raise RuntimeError(f"{name} 就绪超时，日志：{self.runtime / (name + '.log')}")

    def _http(self, port, path):
        if self.alive("network") and port not in (
            self.ports["ragflow"],
            self.ports["web"],
        ):
            return self._probe_namespace(port, path)
        with httpx.Client(timeout=2, trust_env=False) as client:
            return client.get(f"http://127.0.0.1:{port}{path}").status_code == 200

    def _tcp(self, port):
        if self.alive("network"):
            return self._probe_namespace(port)
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True

    def _prepare(self):
        if not self.apptainer.is_file() or not shutil.which("unshare"):
            raise RuntimeError("请先按 docs/RAGFLOW.md 将 Apptainer 1.5.4 安装到用户目录。")
        for directory in (
            self.tools / "images",
            self.tools / "cache",
            self.tools / "tmp",
            self.runtime / "apptainer",
            self.data,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        for name in IMAGES:
            if not self._image(name).is_file():
                subprocess.run(
                    [
                        str(self.apptainer),
                        "pull",
                        str(self._image(name)),
                        "docker://" + IMAGES[name][1],
                    ],
                    env=self.env,
                    check=True,
                )
        for name in (
            "mysql",
            "mysql-run",
            "minio",
            "kvrocks",
            "nats",
            "infinity",
            "logs",
        ):
            (self.data / name).mkdir(parents=True, exist_ok=True)
        conf = self.runtime / "conf"
        conf.mkdir(exist_ok=True, mode=0o700)
        if not (conf / "all_models.json").exists():
            subprocess.run(
                self._command(
                    "ragflow",
                    ["cp", "-a", "/ragflow/conf/.", "/aitea-conf/"],
                    binds=[(conf, "/aitea-conf")],
                ),
                check=True,
                env=self.env,
            )
        values = dict(self.values)
        template = (self.source / "deploy/ragflow/service_conf.yaml.template").read_text()
        # Render values as YAML-compatible JSON scalars. No shell eval of keys.
        for key, value in values.items():
            template = template.replace("'${" + key + "}'", json.dumps(value))
        template = (
            template.replace("host: mysql\n", "host: 127.0.0.1\n")
            .replace("port: 3306", f"port: {self.ports['mysql']}")
            .replace("host: minio:9000", f"host: 127.0.0.1:{self.ports['minio']}")
            .replace("uri: infinity:23817", f"uri: 127.0.0.1:{self.ports['infinity']}")
            .replace("postgres_port: 5432", f"postgres_port: {self.ports['infinity_pg']}")
            .replace("host: kvrocks:6379", f"host: 127.0.0.1:{self.ports['kvrocks']}")
            .replace("host: nats\n", "host: 127.0.0.1\n")
            .replace("port: 4222", f"port: {self.ports['nats']}")
            .replace("host: 0.0.0.0", "host: 127.0.0.1")
            .replace("http_port: 9380", f"http_port: {self.ports['ragflow']}")
        )
        (conf / "service_conf.yaml").write_text(template)
        (conf / "service_conf.yaml").chmod(0o600)
        # Dedicated installation credentials; nothing is written to the image.
        if not (self.runtime / "keys-created").exists():
            subprocess.run(
                [
                    "openssl",
                    "genrsa",
                    "-traditional",
                    "-out",
                    str(conf / "private.pem"),
                    "2048",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [
                    "openssl",
                    "pkey",
                    "-in",
                    str(conf / "private.pem"),
                    "-pubout",
                    "-out",
                    str(conf / "public.pem"),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            (conf / "private.pem").chmod(0o600)
            (self.runtime / "keys-created").touch(mode=0o600)
        infinity = (
            (self.source / "deploy/ragflow/infinity.toml").read_text().replace('"0.0.0.0"', '"127.0.0.1"')
        )
        for key, port in (
            ("postgres_port", self.ports["infinity_pg"]),
            ("http_port", self.ports["infinity_http"]),
            ("client_port", self.ports["infinity"]),
        ):
            infinity = __import__("re").sub(r"(?m)^" + key + r"\s*=\s*\d+", key + " = " + str(port), infinity)
        (self.runtime / "infinity.toml").write_text(infinity)
        (self.runtime / "kvrocks.conf").write_text(
            f"bind 127.0.0.1\nport {self.ports['kvrocks']}\ndir /var/lib/kvrocks\nrequirepass {values['REDIS_PASSWORD']}\ndaemonize no\nlog-level warning\n"
        )
        (self.runtime / "kvrocks.conf").chmod(0o600)
        (self.runtime / "nginx.conf").write_text(
            f"user root;\npid /tmp/aitea-nginx.pid;\nerror_log stderr;\nworker_processes 2;\nevents {{ worker_connections 256; }}\nhttp {{ include /etc/nginx/mime.types; access_log off; client_max_body_size 128m; proxy_read_timeout 180s; server {{ listen 0.0.0.0:{self.ports['web']}; root /ragflow/web/dist; location /api/ {{ proxy_pass http://127.0.0.1:{self.ports['ragflow']}; proxy_buffering off; }} location / {{ try_files $uri $uri/ /index.html; }} }} }}\n"
        )
        script = self.runtime / "api-start.sh"
        script.write_text(
            "#!/bin/sh\nset -eu\ncd /ragflow\nbin/ragflow_server --migrate\nexec bin/ragflow_server --api\n"
        )
        script.chmod(0o700)

    def up(self):
        self._prepare()
        # Check every host port before starting any of this invocation's services.
        ports_by_service = {"ragflow": ["ragflow"], "web": ["web"]}
        for names in ports_by_service.values():
            if not self.alive("nat"):
                for name in names:
                    with socket.socket() as probe:
                        try:
                            probe.bind(("127.0.0.1", self.ports[name]))
                        except OSError:
                            raise RuntimeError(
                                f"端口 {self.ports[name]} 已被占用，未停止占用进程。"
                            ) from None
        self._network()
        p = self.ports
        self._spawn(
            "mysql",
            [
                "docker-entrypoint.sh",
                "mysqld",
                "--bind-address=127.0.0.1",
                f"--port={p['mysql']}",
                "--socket=/var/run/mysqld/mysqld.sock",
                "--character-set-server=utf8mb4",
                "--collation-server=utf8mb4_unicode_ci",
                "--default-authentication-plugin=mysql_native_password",
                "--innodb-buffer-pool-size=256M",
                "--max-connections=200",
                "--init-file=/aitea/init.sql",
            ],
            binds=[
                (self.data / "mysql", "/var/lib/mysql"),
                (self.data / "mysql-run", "/var/run/mysqld"),
                (self.source / "deploy/ragflow/init.sql", "/aitea/init.sql"),
            ],
            environment={
                "MYSQL_ROOT_PASSWORD": self.values["MYSQL_PASSWORD"],
                "MYSQL_DATABASE": "rag_flow",
            },
        )
        self._wait("mysql", lambda: self._tcp(p["mysql"]))
        self._spawn(
            "minio",
            ["silo", "server", "--address", f"127.0.0.1:{p['minio']}", "/data"],
            binds=[(self.data / "minio", "/data")],
            environment={
                "MINIO_ROOT_USER": self.values["MINIO_USER"],
                "MINIO_ROOT_PASSWORD": self.values["MINIO_PASSWORD"],
            },
        )
        self._wait("minio", lambda: self._http(p["minio"], "/minio/health/live"))
        self._spawn(
            "kvrocks",
            ["kvrocks", "-c", "/aitea/kvrocks.conf"],
            binds=[
                (self.data / "kvrocks", "/var/lib/kvrocks"),
                (self.runtime / "kvrocks.conf", "/aitea/kvrocks.conf"),
            ],
        )
        self._wait("kvrocks", lambda: self._tcp(p["kvrocks"]))
        self._spawn(
            "nats",
            [
                "/nats-server",
                "-a",
                "127.0.0.1",
                "-p",
                str(p["nats"]),
                "-js",
                "-sd",
                "/data",
            ],
            binds=[(self.data / "nats", "/data")],
        )
        self._wait("nats", lambda: self._tcp(p["nats"]))
        self._spawn(
            "infinity",
            ["/usr/bin/infinity", "-f", "/aitea/infinity.toml"],
            binds=[
                (self.data / "infinity", "/var/infinity"),
                (self.runtime / "infinity.toml", "/aitea/infinity.toml"),
            ],
        )
        self._wait("infinity", lambda: self._http(p["infinity_http"], "/admin/node/current"))
        self._spawn(
            "admin",
            ["bin/ragflow_server", "--admin", "--init-superuser"],
            image="ragflow",
            binds=[
                (self.runtime / "conf", "/ragflow/conf"),
                (self.data / "logs", "/ragflow/logs"),
            ],
            pwd="/ragflow",
            root_user=True,
            environment={
                "DOC_ENGINE": "infinity",
                "DB_TYPE": "mysql",
                "LD_LIBRARY_PATH": "/usr/lib/x86_64-linux-gnu/",
            },
        )
        self._wait("admin", lambda: self._tcp(9381))
        self._spawn(
            "ragflow",
            ["/aitea/api-start.sh"],
            binds=[
                (self.runtime / "conf", "/ragflow/conf"),
                (self.data / "logs", "/ragflow/logs"),
                (self.runtime / "api-start.sh", "/aitea/api-start.sh"),
            ],
            pwd="/ragflow",
            root_user=True,
            environment={
                "DOC_ENGINE": "infinity",
                "DB_TYPE": "mysql",
                "LD_LIBRARY_PATH": "/usr/lib/x86_64-linux-gnu/",
            },
        )
        self._wait(
            "ragflow",
            lambda: self._http(p["ragflow"], "/api/v1/system/healthz"),
            timeout=300,
        )
        self._spawn(
            "web",
            ["/usr/sbin/nginx", "-c", "/aitea/nginx.conf", "-g", "daemon off;"],
            image="ragflow",
            binds=[(self.runtime / "nginx.conf", "/aitea/nginx.conf")],
            root_user=True,
        )
        self._wait("web", lambda: self._http(p["web"], "/"))
        print(
            f"RAGFlow 已就绪：http://127.0.0.1:{p['web']}；API：http://127.0.0.1:{p['ragflow']}",
            flush=True,
        )
        return 0

    def stop(self, only=None):
        for name in (
            "web",
            "ragflow",
            "admin",
            "infinity",
            "nats",
            "kvrocks",
            "minio",
            "mysql",
            "nat",
            "network",
        ):
            if only is not None and name not in only:
                continue
            if self.alive(name):
                saved = self.state[name]
                if os.getpgid(saved["pid"]) == saved["pid"]:
                    os.killpg(saved["pid"], signal.SIGTERM)
                    deadline = time.monotonic() + 20
                    while self.alive(name) and time.monotonic() < deadline:
                        time.sleep(0.1)
                    if self.alive(name):
                        raise RuntimeError(f"{name} 尚未停止；未强制终止，以保护数据。")
            self.state.pop(name, None)
            self._save()
        print("当前项目的 RAGFlow 已停止，资料、配置和索引已保留。")
        return 0

    def status(self):
        for name in ("network", "nat", *IMAGES, "admin", "web"):
            print(name + ("：运行中" if self.alive(name) else "：未运行"))
        return 0
