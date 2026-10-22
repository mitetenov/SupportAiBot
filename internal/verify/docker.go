// Package verify validates Docker delivery using isolated local fixtures.
package verify

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

func command(ctx context.Context, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, args[0], args[1:]...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, e := cmd.Output()
	if e != nil {
		return nil, fmt.Errorf("%s %s failed: %s", args[0], strings.Join(args[1:min(3, len(args))], " "), strings.TrimSpace(stderr.String()))
	}
	return out, nil
}
func docker(ctx context.Context, args ...string) ([]byte, error) {
	return command(ctx, append([]string{"docker"}, args...)...)
}
func cleanup(args ...string) { _, _ = docker(context.Background(), args...) }

type Image struct {
	Architecture string
	Size         int64
	RootFS       struct{ Layers []string }
	Config       struct {
		User       string
		Entrypoint []string
	}
	State struct {
		ExitCode int
		Running  bool
	}
	HostConfig      struct{ ReadonlyRootfs bool }
	NetworkSettings struct {
		Ports map[string][]struct{ HostPort string }
	}
}

func inspect(ctx context.Context, name string, image bool) (Image, error) {
	args := []string{"inspect", name}
	if image {
		args = []string{"image", "inspect", name}
	}
	raw, e := docker(ctx, args...)
	if e != nil {
		return Image{}, e
	}
	var items []Image
	if e = json.Unmarshal(raw, &items); e != nil || len(items) != 1 {
		return Image{}, fmt.Errorf("invalid image/container inspection")
	}
	return items[0], nil
}
func archive(ctx context.Context, image string) (map[string][]byte, error) {
	raw, e := docker(ctx, "create", image)
	if e != nil {
		return nil, e
	}
	id := strings.TrimSpace(string(raw))
	defer cleanup("rm", "-v", id)
	raw, e = docker(ctx, "export", id)
	if e != nil {
		return nil, e
	}
	r := tar.NewReader(bytes.NewReader(raw))
	out := map[string][]byte{}
	for {
		h, e := r.Next()
		if e == io.EOF {
			break
		}
		if e != nil {
			return nil, e
		}
		name := strings.TrimPrefix(h.Name, "./")
		out[name] = nil
		if name == "etc/ssl/certs/ca-certificates.crt" || name == "app/supportbot" {
			out[name], e = io.ReadAll(r)
			if e != nil {
				return nil, e
			}
		}
	}
	return out, nil
}
func goBinary() string {
	if p := os.Getenv("GO_BINARY"); p != "" {
		return p
	}
	return "go"
}
func writeJSON(path string, value any) error {
	raw, e := json.MarshalIndent(value, "", "  ")
	if e != nil {
		return e
	}
	return os.WriteFile(path, append(raw, '\n'), 0600)
}
func copyContext(src, dst string) error {
	for _, name := range []string{"Dockerfile", ".dockerignore", "go.mod", "go.sum", "cmd", "internal", "faq"} {
		e := filepath.WalkDir(filepath.Join(src, name), func(path string, entry os.DirEntry, e error) error {
			if e != nil {
				return e
			}
			rel, e := filepath.Rel(src, path)
			if e != nil {
				return e
			}
			target := filepath.Join(dst, rel)
			if entry.IsDir() {
				return os.MkdirAll(target, 0755)
			}
			if entry.Type()&os.ModeSymlink != 0 {
				return fmt.Errorf("unexpected symlink in build context")
			}
			raw, e := os.ReadFile(path)
			if e != nil {
				return e
			}
			return os.WriteFile(target, raw, 0644)
		})
		if e != nil {
			return e
		}
	}
	return nil
}

type BinaryMetadata struct {
	SHA256 string `json:"sha256"`
	Mtime  int64  `json:"mtime"`
	Mode   int64  `json:"mode"`
	UID    int    `json:"uid"`
	GID    int    `json:"gid"`
}

func binaryMetadata(ctx context.Context, image string) (BinaryMetadata, error) {
	raw, e := docker(ctx, "create", image)
	if e != nil {
		return BinaryMetadata{}, e
	}
	id := strings.TrimSpace(string(raw))
	defer cleanup("rm", "-v", id)
	raw, e = docker(ctx, "cp", id+":/app/supportbot", "-")
	if e != nil {
		return BinaryMetadata{}, e
	}
	reader := tar.NewReader(bytes.NewReader(raw))
	header, e := reader.Next()
	if e != nil {
		return BinaryMetadata{}, e
	}
	body, e := io.ReadAll(reader)
	if e != nil {
		return BinaryMetadata{}, e
	}
	sum := sha256.Sum256(body)
	return BinaryMetadata{SHA256: hex.EncodeToString(sum[:]), Mtime: header.ModTime.Unix(), Mode: header.Mode, UID: header.Uid, GID: header.Gid}, nil
}
