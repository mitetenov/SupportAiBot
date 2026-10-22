package verify

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

var stepLabel = regexp.MustCompile(`^(#\d+) \[[^\]]+\] (.+)$`)
var stepStatus = regexp.MustCompile(`^(#\d+) (CACHED|DONE)(?: |$)`)

func Steps(log string) map[string]string {
	labels, status := map[string]string{}, map[string]string{}
	for _, line := range strings.Split(log, "\n") {
		if m := stepLabel.FindStringSubmatch(line); m != nil {
			labels[m[1]] = m[2]
		}
		if m := stepStatus.FindStringSubmatch(line); m != nil {
			status[m[1]] = m[2]
		}
	}
	out := map[string]string{}
	for id, label := range labels {
		kind := ""
		switch {
		case strings.Contains(label, "RUN go mod download"):
			kind = "modules"
		case strings.Contains(label, "go build -mod=readonly"):
			kind = "compile"
		case strings.Contains(label, "COPY --link faq/"):
			kind = "faq"
		case strings.Contains(label, "COPY --link --from=builder"):
			kind = "binary"
		case strings.Contains(label, "COPY --link --from=runtime-files"), strings.Contains(label, "RUN mkdir -p /rootfs/etc/ssl/certs"):
			kind = "ca_identity"
		}
		if kind != "" {
			out[kind] = status[id]
		}
	}
	return out
}

type CacheRecord struct {
	BinaryMetadata   BinaryMetadata    `json:"binary_metadata"`
	ImageBytes       int64             `json:"image_bytes"`
	Layers           []string          `json:"layers"`
	Seconds          float64           `json:"seconds"`
	Steps            map[string]string `json:"steps"`
	ModuleRedownload bool              `json:"go_module_redownload_messages"`
}

func Cache(ctx context.Context, image, resultDir string) (map[string]CacheRecord, error) {
	root, e := os.Getwd()
	if e != nil {
		return nil, e
	}
	dir, e := os.MkdirTemp("", "supportbot-cache-")
	if e != nil {
		return nil, e
	}
	defer os.RemoveAll(dir)
	if resultDir == "" {
		resultDir = filepath.Join(os.TempDir(), fmt.Sprintf("supportbot-cache-results-%d", time.Now().UnixNano()))
	}
	if e = os.MkdirAll(resultDir, 0755); e != nil {
		return nil, e
	}
	records := map[string]CacheRecord{}
	nonce := fmt.Sprint(time.Now().UnixNano())
	builder := "supportbot-cache-" + nonce
	tags := []string{}
	defer func() {
		for _, tag := range tags {
			cleanup("image", "rm", tag)
		}
	}()
	build := func(name, path string, extra ...string) (CacheRecord, error) {
		tag := image
		if name != "warm" && name != "unchanged" {
			tag = "supportbot-go:cache-" + nonce + "-" + name
			tags = append(tags, tag)
		}
		args := []string{"buildx", "build", "--load", "--progress=plain", "-t", tag}
		args = append(args, extra...)
		args = append(args, path)
		cmd := exec.CommandContext(ctx, "docker", args...)
		var log bytes.Buffer
		cmd.Stdout = &log
		cmd.Stderr = &log
		started := time.Now()
		err := cmd.Run()
		_ = os.WriteFile(filepath.Join(resultDir, name+".log"), log.Bytes(), 0600)
		if err != nil {
			return CacheRecord{}, fmt.Errorf("cache %s build failed; see %s", name, resultDir)
		}
		cfg, e := inspect(ctx, tag, true)
		if e != nil {
			return CacheRecord{}, e
		}
		metadata, e := binaryMetadata(ctx, tag)
		if e != nil {
			return CacheRecord{}, e
		}
		record := CacheRecord{BinaryMetadata: metadata, ImageBytes: cfg.Size, Layers: cfg.RootFS.Layers, Seconds: time.Since(started).Seconds(), Steps: Steps(log.String()), ModuleRedownload: strings.Contains(log.String(), "go: downloading")}
		records[name] = record
		if e = writeJSON(filepath.Join(resultDir, "progress.json"), records); e != nil {
			return record, e
		}
		return record, nil
	}
	if _, e = build("warm", root); e != nil {
		return nil, e
	}
	unchanged, e := build("unchanged", root)
	if e != nil {
		return nil, e
	}
	if len(unchanged.Steps) != 5 {
		return nil, fmt.Errorf("cache parser did not observe every required layer")
	}
	for _, status := range unchanged.Steps {
		if status != "CACHED" {
			return nil, fmt.Errorf("unchanged build was not cached")
		}
	}
	contextDir := filepath.Join(dir, "context")
	if e = os.Mkdir(contextDir, 0755); e != nil {
		return nil, e
	}
	reset := func() error { return copyContext(root, contextDir) }
	if e = reset(); e != nil {
		return nil, e
	}
	source := filepath.Join(contextDir, "internal/config/config.go")
	appendSource := func(kind string) error {
		f, e := os.OpenFile(source, os.O_APPEND|os.O_WRONLY, 0)
		if e != nil {
			return e
		}
		defer f.Close()
		_, e = fmt.Fprintln(f, "\n// Cache "+kind+" probe "+nonce)
		return e
	}
	if e = appendSource("source"); e != nil {
		return nil, e
	}
	if _, e = build("source", contextDir); e != nil {
		return nil, e
	}
	if e = reset(); e != nil {
		return nil, e
	}
	asset := filepath.Join(contextDir, "faq/faq.json")
	raw, e := os.ReadFile(asset)
	if e != nil {
		return nil, e
	}
	raw, e = probeFAQ(raw, nonce)
	if e != nil {
		return nil, e
	}
	if e = os.WriteFile(asset, raw, 0644); e != nil {
		return nil, e
	}
	if _, e = build("asset", contextDir); e != nil {
		return nil, e
	}
	if e = reset(); e != nil {
		return nil, e
	}
	if _, e = command(ctx, goBinary(), "-C", contextDir, "get", "github.com/joho/godotenv@v1.5.0"); e != nil {
		return nil, e
	}
	manifest := filepath.Join(contextDir, "go.mod")
	module, e := os.OpenFile(manifest, os.O_APPEND|os.O_WRONLY, 0)
	if e != nil {
		return nil, e
	}
	_, writeErr := fmt.Fprintln(module, "\n// Dependency cache probe "+nonce)
	closeErr := module.Close()
	if writeErr != nil {
		return nil, writeErr
	}
	if closeErr != nil {
		return nil, closeErr
	}
	if _, e = build("dependency", contextDir); e != nil {
		return nil, e
	}
	exported := filepath.Join(dir, "exported")
	if _, e = build("export", root, "--cache-to", "type=local,dest="+exported+",mode=max"); e != nil {
		return nil, e
	}
	if _, e = docker(ctx, "buildx", "create", "--name", builder, "--driver", "docker-container"); e != nil {
		return nil, e
	}
	defer cleanup("buildx", "rm", "--force", builder)
	if e = reset(); e != nil {
		return nil, e
	}
	if e = appendSource("fresh-builder-source"); e != nil {
		return nil, e
	}
	if _, e = build("fresh-import", contextDir, "--builder", builder, "--cache-from", "type=local,src="+exported); e != nil {
		return nil, e
	}
	require := func(ok bool, message string) error {
		if !ok {
			return fmt.Errorf("cache invariant failed: %s; logs %s", message, resultDir)
		}
		return nil
	}
	for _, name := range []string{"source", "asset", "dependency"} {
		r := records[name]
		if len(r.Layers) < 3 || len(unchanged.Layers) < 3 {
			return nil, fmt.Errorf("unexpected runtime layer layout")
		}
		if e = require(r.Layers[0] == unchanged.Layers[0], name+" changed CA layer"); e != nil {
			return nil, e
		}
	}
	for _, name := range []string{"source", "dependency"} {
		r := records[name]
		if e = require(r.Layers[len(r.Layers)-2] == unchanged.Layers[len(unchanged.Layers)-2], name+" changed FAQ layer"); e != nil {
			return nil, e
		}
	}
	a, s, d, f := records["asset"], records["source"], records["dependency"], records["fresh-import"]
	checks := []struct {
		ok      bool
		message string
	}{{s.Steps["modules"] == "CACHED" && s.Steps["compile"] == "DONE", "source compile/dependency isolation"}, {a.Steps["compile"] == "CACHED" && a.Steps["faq"] == "DONE", "FAQ compile isolation"}, {a.Layers[len(a.Layers)-1] == unchanged.Layers[len(unchanged.Layers)-1], "FAQ changed binary layer"}, {d.Steps["modules"] == "DONE" && d.Steps["compile"] == "DONE", "dependency invalidation"}, {f.Steps["modules"] == "CACHED" && f.Steps["compile"] == "DONE" && !f.ModuleRedownload, "fresh builder cache import"}}
	for _, c := range checks {
		if e = require(c.ok, c.message); e != nil {
			return nil, e
		}
	}
	if e = writeJSON(filepath.Join(resultDir, "results.json"), records); e != nil {
		return nil, e
	}
	return records, nil
}

func probeFAQ(raw []byte, nonce string) ([]byte, error) {
	var entries []map[string]any
	if e := json.Unmarshal(raw, &entries); e != nil {
		return nil, e
	}
	if len(entries) == 0 {
		return nil, fmt.Errorf("cache FAQ fixture is empty")
	}
	entries[0]["__cache_probe"] = nonce
	return json.Marshal(entries)
}
