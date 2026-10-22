package verify

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

type fixtureAPI struct {
	mu   sync.Mutex
	sent bool
	seen map[string]bool
}

func (f *fixtureAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	reply := func(v any) { _ = json.NewEncoder(w).Encode(v) }
	if r.Method == http.MethodDelete {
		reply(map[string]any{})
		return
	}
	if r.Method != http.MethodPost {
		w.WriteHeader(405)
		reply(map[string]any{})
		return
	}
	var req map[string]any
	if json.NewDecoder(r.Body).Decode(&req) != nil {
		w.WriteHeader(400)
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.URL.Path == "/mcp" {
		var result any
		switch req["method"] {
		case "initialize":
			params, _ := req["params"].(map[string]any)
			result = map[string]any{"protocolVersion": params["protocolVersion"], "capabilities": map[string]any{"tools": map[string]any{}}, "serverInfo": map[string]any{"name": "local-smoke", "version": "1"}}
		case "tools/list":
			f.seen["mcp"] = true
			result = map[string]any{"tools": []any{}}
		case "ping":
			result = map[string]any{}
		default:
			if req["id"] == nil {
				w.WriteHeader(202)
				return
			}
			reply(map[string]any{"jsonrpc": "2.0", "id": req["id"], "error": map[string]any{"code": -32601, "message": "unsupported fixture method"}})
			return
		}
		reply(map[string]any{"jsonrpc": "2.0", "id": req["id"], "result": result})
		return
	}
	if strings.HasSuffix(r.URL.Path, "/embeddings") {
		inputs, _ := req["input"].([]any)
		if input, ok := req["input"].(string); ok {
			inputs = []any{input}
		}
		dim := 1536
		if n, ok := req["dimensions"].(float64); ok {
			dim = int(n)
		}
		if dim < 1 || dim > 16000 {
			w.WriteHeader(400)
			return
		}
		vector := make([]float64, dim)
		vector[0] = 1
		data := []any{}
		for i := range inputs {
			data = append(data, map[string]any{"index": i, "embedding": vector})
		}
		reply(map[string]any{"data": data})
		return
	}
	if strings.HasSuffix(r.URL.Path, "/responses") {
		reply(map[string]any{"output": []any{map[string]any{"type": "message", "role": "assistant", "content": []any{map[string]any{"type": "output_text", "text": "Local fixture answer"}}}}, "usage": map[string]any{"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}})
		return
	}
	parts := strings.Split(r.URL.Path, "/")
	method := parts[len(parts)-1]
	var result any = true
	switch method {
	case "getUpdates":
		result = []any{}
		if !f.sent {
			f.sent = true
			updates := []any{}
			for i, message := range []string{"/help", "Как оплатить подписку?"} {
				id := int64(5000000001 + i)
				updates = append(updates, map[string]any{"update_id": i + 1, "message": map[string]any{"message_id": i + 1, "chat": map[string]any{"id": id, "type": "private"}, "from": map[string]any{"id": id, "first_name": "Fixture"}, "text": message}})
			}
			result = updates
		} else {
			time.Sleep(100 * time.Millisecond)
		}
	case "setMyCommands":
		f.seen["commands"] = true
	case "sendMessage":
		if req["chat_id"] == float64(5000000001) {
			f.seen["help"] = true
		}
		if req["chat_id"] == float64(5000000002) && strings.Contains(fmt.Sprint(req["text"]), "Local fixture answer") {
			f.seen["answer"] = true
		}
		result = map[string]any{"message_id": 90}
	case "createForumTopic":
		result = map[string]any{"message_thread_id": 77}
	case "copyMessage":
		result = map[string]any{"message_id": 91}
	}
	reply(map[string]any{"ok": true, "result": result})
}
func pause(ctx context.Context, d time.Duration) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(d):
		return nil
	}
}
func Smoke(ctx context.Context, image string) (map[string]any, error) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Minute)
	defer cancel()
	nonce := fmt.Sprint(time.Now().UnixNano())
	network := "supportbot-smoke-" + nonce
	pg := network + "-pg"
	bot := network + "-bot"
	if _, e := docker(ctx, "network", "create", network); e != nil {
		return nil, e
	}
	defer cleanup("network", "rm", network)
	if _, e := docker(ctx, "run", "-d", "--name", pg, "--network", network, "--network-alias", "pg", "-e", "POSTGRES_USER=parity", "-e", "POSTGRES_PASSWORD=local-parity-only", "-e", "POSTGRES_DB=smoke", "pgvector/pgvector:pg17"); e != nil {
		return nil, e
	}
	defer cleanup("rm", "-f", "-v", pg)
	ready := false
	for range 60 {
		if _, e := docker(ctx, "exec", pg, "pg_isready", "-U", "parity", "-d", "smoke"); e == nil {
			ready = true
			break
		}
		if e := pause(ctx, 500*time.Millisecond); e != nil {
			return nil, e
		}
	}
	if !ready {
		return nil, fmt.Errorf("smoke PostgreSQL did not become ready")
	}
	api := &fixtureAPI{seen: map[string]bool{"help": false, "answer": false, "commands": false, "mcp": false}}
	listener, e := net.Listen("tcp", "0.0.0.0:0")
	if e != nil {
		return nil, e
	}
	server := &http.Server{Handler: api, ReadHeaderTimeout: 5 * time.Second}
	go func() { _ = server.Serve(listener) }()
	defer server.Close()
	endpoint := fmt.Sprintf("http://host.docker.internal:%d", listener.Addr().(*net.TCPAddr).Port)
	env := map[string]string{"TELEGRAM_BOT_TOKEN": "123456789:local-fixture-not-a-real-bot-token", "TELEGRAM_SUPPORT_GROUP_CHAT_ID": "-100100", "TELEGRAM_API_BASE_URL": endpoint, "TELEGRAM_BUFFER_WINDOW_MS": "20", "PGVECTOR_HOST": "pg", "PGVECTOR_PORT": "5432", "PGVECTOR_DB": "smoke", "PGVECTOR_USER": "parity", "PGVECTOR_PASSWORD": "local-parity-only", "LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-local-fixture", "OPENAI_BASE_URL": endpoint + "/v1", "EMBEDDING_PROVIDER": "openai", "REMNAWAVE_MCP_URL": endpoint + "/mcp", "BOT_LOG_LEVEL": "ERROR"}
	args := []string{"run", "-d", "--read-only", "--name", bot, "--network", network, "--add-host=host.docker.internal:host-gateway", "-p", "127.0.0.1::8080"}
	for k, v := range env {
		args = append(args, "-e", k+"="+v)
	}
	args = append(args, image)
	if _, e = docker(ctx, args...); e != nil {
		return nil, e
	}
	defer cleanup("rm", "-f", "-v", bot)
	config, e := inspect(ctx, bot, false)
	if e != nil {
		return nil, e
	}
	ports := config.NetworkSettings.Ports["8080/tcp"]
	if len(ports) == 0 {
		return nil, fmt.Errorf("health port missing")
	}
	healthURL := "http://127.0.0.1:" + ports[0].HostPort + "/health"
	healthy := false
	client := &http.Client{Timeout: time.Second}
	for range 225 {
		resp, err := client.Get(healthURL)
		if err == nil {
			var state map[string]string
			_ = json.NewDecoder(resp.Body).Decode(&state)
			resp.Body.Close()
			healthy = resp.StatusCode == 200 && state["status"] == "UP"
		}
		api.mu.Lock()
		seen := true
		for _, ok := range api.seen {
			seen = seen && ok
		}
		api.mu.Unlock()
		if healthy && seen {
			break
		}
		if e = pause(ctx, 200*time.Millisecond); e != nil {
			return nil, e
		}
	}
	api.mu.Lock()
	seen := map[string]bool{}
	all := true
	for k, v := range api.seen {
		seen[k] = v
		all = all && v
	}
	api.mu.Unlock()
	if !healthy || !all {
		return nil, fmt.Errorf("smoke failed: health=%t fixtures=%v", healthy, seen)
	}
	if _, e = docker(ctx, "exec", bot, "/app/supportbot", "healthcheck"); e != nil {
		return nil, e
	}
	files, e := archive(ctx, image)
	if e != nil {
		return nil, e
	}
	for _, p := range []string{"app/supportbot", "app/faq/faq.json", "app/faq/images/happ-buttons.png", "etc/ssl/certs/ca-certificates.crt"} {
		if _, ok := files[p]; !ok {
			return nil, fmt.Errorf("missing runtime asset %s", p)
		}
	}
	for p := range files {
		if strings.HasPrefix(p, "app/app/") || strings.HasSuffix(p, "/python") || strings.HasSuffix(p, "/python3") {
			return nil, fmt.Errorf("obsolete runtime in image")
		}
	}
	if !config.HostConfig.ReadonlyRootfs || config.Config.User != "100:101" {
		return nil, fmt.Errorf("runtime privileges drift")
	}
	history := func() (string, error) {
		raw, e := docker(ctx, "exec", pg, "psql", "-U", "parity", "-d", "smoke", "-Atc", "SELECT COUNT(*) FROM chat_messages WHERE telegram_id=5000000002")
		return strings.TrimSpace(string(raw)), e
	}
	count, e := history()
	if e != nil || count != "2" {
		return nil, fmt.Errorf("history was not persisted exactly once")
	}
	if _, e = docker(ctx, "stop", "-t", "30", bot); e != nil {
		return nil, e
	}
	stopped, e := inspect(ctx, bot, false)
	if e != nil {
		return nil, e
	}
	if stopped.State.ExitCode != 0 {
		return nil, fmt.Errorf("ungraceful container shutdown")
	}
	if _, e = docker(ctx, "start", bot); e != nil {
		return nil, e
	}
	restarted := false
	for range 100 {
		if _, e := docker(ctx, "exec", bot, "/app/supportbot", "healthcheck"); e == nil {
			restarted = true
			break
		}
		if e = pause(ctx, 200*time.Millisecond); e != nil {
			return nil, e
		}
	}
	count, e = history()
	if !restarted || e != nil || count != "2" {
		return nil, fmt.Errorf("restart persistence failed")
	}
	if _, e = docker(ctx, "stop", "-t", "30", bot); e != nil {
		return nil, e
	}
	return map[string]any{"native_image": "passed", "image": image, "readonly_rootfs": true, "health": healthy, "external_api_fixtures": seen, "persisted_history": 2, "restart_persistence": true, "graceful_exit": stopped.State.ExitCode}, nil
}
