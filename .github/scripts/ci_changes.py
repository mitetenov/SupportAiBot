"""Select CI work from the exact tested diff; unknown inputs run every gate."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def classify(paths: list[str]) -> dict[str, bool]:
    result = dict.fromkeys(("go", "python", "image"), False)
    for path in paths:
        if path.startswith(("cmd/", "internal/")):
            result["go"] = True
            if not path.endswith("_test.go") and "testdata" not in Path(path).parts:
                result["image"] = True
        elif path in {"go.mod", "go.sum", "Dockerfile", ".dockerignore"}:
            result["go"] = result["image"] = True
        elif path.startswith("faq/"):
            result = dict.fromkeys(result, True)
        elif path == "pyproject.toml":
            # The image release major is read from this file too.
            result["python"] = result["image"] = True
        elif path.startswith(("app/", "tests/")) or path == "uv.lock":
            result["python"] = True
        elif path.startswith("docs/") or path in {"README.md", "LICENSE", "LICENSE.md"}:
            continue
        else:
            # Includes workflow/scripts changes and future, unclassified inputs.
            return dict.fromkeys(result, True)
    return result


def select(event_name: str, event: dict, head: str) -> dict[str, bool]:
    full = dict.fromkeys(("go", "python", "image"), True)
    if event_name == "pull_request":
        base = event.get("pull_request", {}).get("base", {}).get("sha")
    elif event_name == "push":
        base = event.get("before")
    else:
        return full
    if not base or not head or set(base) == {"0"}:
        return full
    try:
        # No rename detection: moving a file between domains checks BOTH paths.
        diff = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", base, head, "--"],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        # Deleted/unreachable pre-force-push commits must never skip checks.
        return full
    return classify([os.fsdecode(path) for path in diff.stdout.split(b"\0") if path])


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    selected = select(os.environ["GITHUB_EVENT_NAME"], event, os.environ["GITHUB_SHA"])
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for name, enabled in selected.items():
            print(f"{name}={str(enabled).lower()}", file=output)
    print(json.dumps(selected, sort_keys=True))


if __name__ == "__main__":
    main()
