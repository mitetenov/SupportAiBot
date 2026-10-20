"""Run the native Go image against local stub APIs and a disposable PostgreSQL DB.

Requires Docker plus the local supportbot-go-parity-pg fixture container. Never loads .env.
"""

import http.server
import io
import json
import os
import subprocess
import tarfile
import threading
import time
import urllib.request

NAME = "supportbot-go-native-smoke"
IMAGE = os.environ.get("SUPPORTBOT_TEST_IMAGE", "supportbot-go:parity")
PG = "supportbot-go-parity-pg"
DB = "supportbot_docker_parity"
seen = {"help": False, "answer": False, "commands": False, "mcp": False}
lock = threading.Lock()
sent = False


def docker(*args, check=True):
    return subprocess.run(["docker", *args], text=True, capture_output=True, check=check)


class API(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def reply(self, payload, code=200):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply({}, 405)

    def do_DELETE(self):
        self.reply({})

    def do_POST(self):
        global sent
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        request = json.loads(raw or b"{}")
        if self.path == "/mcp":
            method = request.get("method") or self.headers.get("Mcp-Method", "")
            if method == "initialize":
                result = {
                    "protocolVersion": request["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "local-smoke", "version": "1"},
                }
            elif method == "tools/list":
                seen["mcp"] = True
                result = {"tools": []}
            elif method == "ping":
                result = {}
            else:
                if "id" in request:
                    self.reply(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "error": {"code": -32601, "message": "unsupported fixture method"},
                        }
                    )
                else:
                    self.reply({}, 202)
                return
            self.reply({"jsonrpc": "2.0", "id": request.get("id"), "result": result})
            return
        if self.path.endswith("/embeddings"):
            inputs = request.get("input", [])
            if isinstance(inputs, str):
                inputs = [inputs]
            dimension = request.get("dimensions", 1536)
            vector = [1.0] + [0.0] * (dimension - 1)
            self.reply({"data": [{"index": i, "embedding": vector} for i in range(len(inputs))]})
            return
        if self.path.endswith("/responses"):
            self.reply(
                {
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Local fixture answer"}],
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                }
            )
            return
        method = self.path.rsplit("/", 1)[-1]
        result = True
        if method == "getUpdates":
            with lock:
                emit = not sent
                sent = True
            if emit:
                result = [
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 1,
                            "chat": {"id": 5000000001, "type": "private"},
                            "from": {"id": 5000000001, "first_name": "Fixture"},
                            "text": "/help",
                        },
                    },
                    {
                        "update_id": 2,
                        "message": {
                            "message_id": 2,
                            "chat": {"id": 5000000002, "type": "private"},
                            "from": {"id": 5000000002, "first_name": "Fixture"},
                            "text": "Как оплатить подписку?",
                        },
                    },
                ]
            else:
                time.sleep(0.1)
                result = []
        elif method == "setMyCommands":
            seen["commands"] = True
        elif method == "sendMessage":
            if request.get("chat_id") == 5000000001:
                seen["help"] = True
            if request.get("chat_id") == 5000000002 and "Local fixture answer" in request.get(
                "text", ""
            ):
                seen["answer"] = True
            result = {"message_id": 90}
        elif method == "createForumTopic":
            result = {"message_thread_id": 77}
        elif method == "copyMessage":
            result = {"message_id": 91}
        self.reply({"ok": True, "result": result})


def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), API)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://host.docker.internal:{server.server_port}"
    docker("exec", PG, "psql", "-U", "parity", "-d", "postgres", "-c", f"CREATE DATABASE {DB}")
    try:
        env = {
            "TELEGRAM_BOT_TOKEN": "123456789:local-fixture-not-a-real-bot-token",
            "TELEGRAM_SUPPORT_GROUP_CHAT_ID": "-100100",
            "TELEGRAM_API_BASE_URL": endpoint,
            "TELEGRAM_BUFFER_WINDOW_MS": "20",
            "PGVECTOR_HOST": "host.docker.internal",
            "PGVECTOR_PORT": "64641",
            "PGVECTOR_DB": DB,
            "PGVECTOR_USER": "parity",
            "PGVECTOR_PASSWORD": "local-parity-only",
            "LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-local-fixture",
            "OPENAI_BASE_URL": endpoint + "/v1",
            "EMBEDDING_PROVIDER": "openai",
            "REMNAWAVE_MCP_URL": endpoint + "/mcp",
            "BOT_LOG_LEVEL": "ERROR",
        }
        args = ["run", "-d", "--read-only", "--name", NAME, "-p", "127.0.0.1::8080"]
        for key, value in env.items():
            args.extend(["-e", f"{key}={value}"])
        args.append(IMAGE)
        docker(*args)
        port = json.loads(docker("inspect", NAME).stdout)[0]["NetworkSettings"]["Ports"][
            "8080/tcp"
        ][0]["HostPort"]
        deadline = time.monotonic() + 45
        healthy = False
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1
                ) as response:
                    healthy = json.load(response) == {"status": "UP"}
            except OSError:
                pass
            if healthy and all(seen.values()):
                break
            time.sleep(0.2)
        assert healthy and all(seen.values()), {
            "health": healthy,
            "seen": seen,
            "logs": docker("logs", NAME).stdout,
        }
        docker("exec", NAME, "/app/supportbot", "healthcheck")
        # Inspect the filesystem from outside: minimal runtimes need no shell.
        exported = subprocess.run(
            ["docker", "export", NAME], capture_output=True, check=True
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(exported)) as archive:
            paths = {member.name.lstrip("./") for member in archive.getmembers()}
        assert "app/supportbot" in paths and "app/faq/faq.json" in paths
        assert "app/faq/images/happ-buttons.png" in paths
        assert "etc/ssl/certs/ca-certificates.crt" in paths
        assert not any(
            path.startswith("app/app/") or path.rsplit("/", 1)[-1] in {"python", "python3"}
            for path in paths
        )
        config = json.loads(docker("inspect", NAME).stdout)[0]
        assert config["HostConfig"]["ReadonlyRootfs"]
        assert config["Config"]["User"] not in {"", "root", "0", "0:0"}
        usage = docker(
            "exec",
            PG,
            "psql",
            "-U",
            "parity",
            "-d",
            DB,
            "-Atc",
            "SELECT COUNT(*) FROM chat_messages WHERE telegram_id=5000000002",
        ).stdout.strip()
        assert usage == "2", {"persisted_history": usage}
        docker("stop", "-t", "30", NAME)
        exit_code = json.loads(docker("inspect", NAME).stdout)[0]["State"]["ExitCode"]
        assert exit_code == 0, {"exit": exit_code, "logs": docker("logs", NAME).stdout}
        print(
            json.dumps(
                {
                    "native_image": "passed",
                    "image": IMAGE,
                    "readonly_rootfs": True,
                    "health": healthy,
                    "external_api_fixtures": seen,
                    "persisted_history": 2,
                    "graceful_exit": exit_code,
                    "python_runtime": "absent",
                }
            )
        )
    finally:
        docker("rm", "-f", NAME, check=False)
        docker(
            "exec",
            PG,
            "psql",
            "-U",
            "parity",
            "-d",
            "postgres",
            "-c",
            f"DROP DATABASE {DB} WITH (FORCE)",
            check=False,
        )
        server.shutdown()


if __name__ == "__main__":
    main()
