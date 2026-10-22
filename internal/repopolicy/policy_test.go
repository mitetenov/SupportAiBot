package repopolicy

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestRepositoryHasNoRetiredRuntimeOrCI(t *testing.T) {
	root := "../.."
	cmd := exec.Command("git", "-C", root, "ls-files", "-z")
	raw, e := cmd.Output()
	if e != nil {
		t.Fatal(e)
	}
	for _, path := range strings.Split(string(raw), "\x00") {
		if path == "" {
			continue
		}
		if _, e := os.Stat(filepath.Join(root, path)); os.IsNotExist(e) {
			continue
		}
		base := filepath.Base(path)
		if strings.HasSuffix(path, ".py") || strings.HasSuffix(path, ".pyc") || base == "pyproject.toml" || base == "uv.lock" || base == ".python-version" || strings.HasPrefix(base, "requirements") {
			t.Errorf("retired toolchain file: %s", path)
		}
		if strings.HasPrefix(path, ".github/workflows/") {
			body, e := os.ReadFile(filepath.Join(root, path))
			if e != nil {
				t.Fatal(e)
			}
			for _, retired := range []string{"setup-python", "setup-uv", "python3 ", "uv run", "pytest", "pyproject.toml", "mypy", "ruff"} {
				if strings.Contains(string(body), retired) {
					t.Errorf("retired CI tool %q in %s", retired, path)
				}
			}
		}
	}
}
