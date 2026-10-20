"""Measure local Docker size and cache isolation with disposable build contexts.

Keeps supportbot-go:parity and supportbot-go:optimized. Removes its probe images,
BuildKit builder and temporary contexts/cache. No application .env is read.
"""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GO = os.environ.get("GO_BINARY", "go")


def command(*args, log=None):
    started = time.monotonic()
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True)
    output = result.stdout + result.stderr
    if log:
        log.write_text(output)
    if result.returncode:
        raise RuntimeError(output[-12000:])
    return output, round(time.monotonic() - started, 3)


def steps(text):
    labels, statuses = {}, {}
    for line in text.splitlines():
        match = re.match(r"^(#\d+) \[([^\]]+)\] (.+)$", line)
        if match:
            labels[match[1]] = match[3]
        match = re.match(r"^(#\d+) (CACHED|DONE)(?: |$)", line)
        if match:
            statuses[match[1]] = match[2]
    output = {}
    for step, label in labels.items():
        category = None
        if "RUN go mod download" in label:
            category = "modules"
        elif "go build -mod=readonly" in label:
            category = "compile"
        elif "COPY --link faq/" in label:
            category = "faq"
        elif "COPY --link --from=builder" in label:
            category = "binary"
        elif "COPY --link --from=runtime-files" in label:
            category = "ca_identity"
        if category:
            output[category] = statuses.get(step, "UNKNOWN")
    return output


def main():
    output_dir = Path(
        os.environ.get("DOCKER_CACHE_RESULTS", "/private/tmp/supportbot-docker-cache-results")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    records = {}
    nonce = str(time.time_ns())
    images = []
    builder = f"supportbot-cache-probe-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix="supportbot-docker-cache-") as directory:
        temporary = Path(directory)
        context = temporary / "context"
        context.mkdir()

        def reset_context():
            for name in ["Dockerfile", ".dockerignore", "go.mod", "go.sum"]:
                shutil.copy2(ROOT / name, context / name)
            for name in ["cmd", "internal", "faq"]:
                shutil.copytree(ROOT / name, context / name, dirs_exist_ok=True)

        def build(name, path=ROOT, extra=()):
            tag = (
                "supportbot-go:optimized" if name == "unchanged" else f"supportbot-go:cache-{name}"
            )
            if name != "unchanged":
                images.append(tag)
            text, seconds = command(
                "docker",
                "buildx",
                "build",
                "--load",
                "--progress=plain",
                "-t",
                tag,
                *extra,
                str(path),
                log=output_dir / f"{name}.log",
            )
            image = json.loads(command("docker", "image", "inspect", tag)[0])[0]
            container = command("docker", "create", tag)[0].strip()
            try:
                archive = subprocess.run(
                    ["docker", "cp", f"{container}:/app/supportbot", "-"],
                    check=True,
                    capture_output=True,
                ).stdout
                with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                    member = tar.getmembers()[0]
                    binary = tar.extractfile(member).read()
                    metadata = {
                        "sha256": hashlib.sha256(binary).hexdigest(),
                        "mtime": member.mtime,
                        "mode": member.mode,
                        "uid": member.uid,
                        "gid": member.gid,
                    }
            finally:
                command("docker", "rm", container)
            records[name] = {
                "binary_metadata": metadata,
                "layers": image["RootFS"]["Layers"],
                "image_bytes": image["Size"],
                "seconds": seconds,
                "steps": steps(text),
                "go_module_redownload_messages": "go: downloading" in text,
            }
            (output_dir / "progress.json").write_text(json.dumps(records, indent=2) + "\n")
            return records[name]

        try:
            build("unchanged")
            reset_context()
            source = context / "internal/config/config.go"
            source.write_text(
                source.read_text() + f"\n// Docker cache source-only probe {nonce}.\n"
            )
            build("source", context)
            reset_context()
            asset = context / "faq/faq.json"
            payload = json.loads(asset.read_text())
            payload[0]["__cache_probe"] = nonce
            asset.write_text(json.dumps(payload, ensure_ascii=False))
            build("asset", context)
            reset_context()
            command(
                GO,
                "-C",
                str(context),
                "get",
                "github.com/joho/godotenv@v1.5.0",
                log=output_dir / "dependency-change.log",
            )
            manifest = context / "go.mod"
            manifest.write_text(manifest.read_text() + f"\n// Dependency cache probe {nonce}.\n")
            build("dependency", context)

            exported = temporary / "cache"
            build("export", ROOT, ("--cache-to", f"type=local,dest={exported},mode=max"))
            command("docker", "buildx", "create", "--name", builder, "--driver", "docker-container")
            reset_context()
            source.write_text(
                source.read_text() + f"\n// Fresh builder source-only probe {nonce}.\n"
            )
            build(
                "fresh-import",
                context,
                ("--builder", builder, "--cache-from", f"type=local,src={exported}"),
            )

            assert all(v == "CACHED" for v in records["unchanged"]["steps"].values()), records[
                "unchanged"
            ]
            for scenario in ("source", "asset", "dependency"):
                assert records[scenario]["layers"][0] == records["unchanged"]["layers"][0], records[
                    scenario
                ]
            assert records["dependency"]["layers"][-2] == records["unchanged"]["layers"][-2], (
                records["dependency"]
            )
            assert records["source"]["steps"]["modules"] == "CACHED", records["source"]
            assert records["source"]["steps"]["compile"] == "DONE", records["source"]
            assert records["source"]["layers"][-2] == records["unchanged"]["layers"][-2], records[
                "source"
            ]
            assert records["asset"]["steps"]["compile"] == "CACHED", records["asset"]
            assert records["asset"]["layers"][-1] == records["unchanged"]["layers"][-1], records[
                "asset"
            ]
            assert records["asset"]["steps"]["faq"] == "DONE", records["asset"]
            assert records["dependency"]["steps"]["modules"] == "DONE", records["dependency"]
            assert records["dependency"]["steps"]["compile"] == "DONE", records["dependency"]
            assert records["fresh-import"]["steps"]["modules"] == "CACHED", records["fresh-import"]
            assert records["fresh-import"]["steps"]["compile"] == "DONE", records["fresh-import"]
            assert not records["fresh-import"]["go_module_redownload_messages"], records[
                "fresh-import"
            ]
            (output_dir / "results.json").write_text(json.dumps(records, indent=2) + "\n")
            print(json.dumps(records, indent=2))
        finally:
            if not os.environ.get("DOCKER_CACHE_KEEP_PROBES"):
                subprocess.run(["docker", "buildx", "rm", "--force", builder], capture_output=True)
            if not os.environ.get("DOCKER_CACHE_KEEP_PROBES"):
                for tag in images:
                    command("docker", "image", "rm", tag)


if __name__ == "__main__":
    main()
