"""CI selection must save work without letting untested changes pass."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/ci_changes.py"
_spec = importlib.util.spec_from_file_location("ci_changes", SCRIPT)
assert _spec and _spec.loader
ci_changes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_changes)

FULL = {"go": True, "python": True, "image": True}


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["internal/bot/service.go"], {"go": True, "python": False, "image": True}),
        (["internal/bot/service_test.go"], {"go": True, "python": False, "image": False}),
        (["internal/bot/testdata/ref.json"], {"go": True, "python": False, "image": False}),
        (["app/main.py", "tests/test_main.py"], {"go": False, "python": True, "image": False}),
        (["uv.lock"], {"go": False, "python": True, "image": False}),
        (["pyproject.toml"], {"go": False, "python": True, "image": True}),
        (["faq/faq.json"], FULL),
        (["docs/guide.md", "README.md"], {"go": False, "python": False, "image": False}),
        ([".github/workflows/deploy.yml"], FULL),
        (["new-build-input.txt"], FULL),
        (["Dockerfile"], {"go": True, "python": False, "image": True}),
        (["go.sum"], {"go": True, "python": False, "image": True}),
    ],
)
def test_selects_all_affected_domains(paths: list[str], expected: dict[str, bool]) -> None:
    assert ci_changes.classify(paths) == expected


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    git("init", "-q")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "CI fixture")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "README.md").write_text("fixture\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    return tmp_path


def commit() -> str:
    git("add", "-A")
    git("commit", "-qm", "fixture change")
    return git("rev-parse", "HEAD")


def test_push_checks_entire_range_including_deleted_inputs(repository: Path) -> None:
    (repository / "go.sum").write_text("fixture\n")
    base = commit()
    (repository / "go.sum").unlink()
    commit()
    (repository / "README.md").write_text("docs changed afterwards\n")
    head = commit()
    assert ci_changes.select("push", {"before": base}, head) == {
        "go": True,
        "python": False,
        "image": True,
    }


def test_pr_compares_base_to_tested_merge_and_rename_checks_both_domains(repository: Path) -> None:
    (repository / "internal").mkdir()
    (repository / "internal/runtime.txt").write_text("identical renamed content\n")
    base = commit()
    git("switch", "-qc", "feature")
    (repository / "app").mkdir()
    git("mv", "internal/runtime.txt", "app/runtime.txt")
    commit()
    git("switch", "-qc", "base-side", base)
    (repository / "README.md").write_text("base advanced\n")
    merge_base = commit()
    git("merge", "--no-ff", "-qm", "tested merge", "feature")
    event = {"pull_request": {"base": {"sha": merge_base}}}
    assert ci_changes.select("pull_request", event, git("rev-parse", "HEAD")) == FULL


@pytest.mark.parametrize(
    ("event_name", "event"),
    [
        ("workflow_dispatch", {}),
        ("push", {"before": "0" * 40}),
        ("push", {"before": "f" * 40}),
        ("pull_request", {}),
    ],
)
def test_missing_diff_and_manual_runs_require_everything(
    repository: Path, event_name: str, event: dict
) -> None:
    assert ci_changes.select(event_name, event, git("rev-parse", "HEAD")) == FULL


def test_cli_emits_github_outputs_for_docs_only_diff(repository: Path) -> None:
    base = git("rev-parse", "HEAD")
    (repository / "README.md").write_text("only documentation\n")
    head = commit()
    event_path = repository / "event.json"
    event_path.write_text(json.dumps({"before": base}))
    output_path = repository / "outputs"
    subprocess.run(
        [os.sys.executable, str(SCRIPT)],
        check=True,
        env={
            **os.environ,
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_SHA": head,
            "GITHUB_OUTPUT": str(output_path),
        },
        capture_output=True,
    )
    assert output_path.read_text() == "go=false\npython=false\nimage=false\n"
