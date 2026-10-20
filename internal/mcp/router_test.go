package mcp

import (
	"context"
	"encoding/json"
	"sync"
	"testing"
)

type mockClient struct {
	name    string
	tools   []Tool
	mu      sync.Mutex
	calls   []string
	args    []map[string]any
	results map[string]string
}

func (c *mockClient) ServerName() string { return c.name }
func (c *mockClient) ListTools() []Tool  { return c.tools }
func (c *mockClient) CallTool(_ context.Context, n string, a map[string]any) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.calls = append(c.calls, n)
	c.args = append(c.args, a)
	if s, ok := c.results[n]; ok {
		return s, nil
	}
	return `{"ok":true}`, nil
}
func schema(keys map[string]string) map[string]any {
	p := map[string]any{}
	for k, t := range keys {
		p[k] = map[string]any{"type": t}
	}
	return map[string]any{"type": "object", "properties": p}
}
func TestRouterIdentityOwnersAndReadOnly(t *testing.T) {
	bed := &mockClient{name: "bedolaga", tools: []Tool{{Name: "bedolaga_user_get", InputSchema: schema(map[string]string{"telegram_id": "integer", "user_id": "integer"})}}}
	rem := &mockClient{name: "remnawave", tools: []Tool{{Name: "users_get_by_telegram_id", InputSchema: schema(map[string]string{"telegramId": "number"})}, {Name: "hwid_device_delete", InputSchema: schema(map[string]string{"telegramId": "number", "userId": "number"})}, {Name: "nodes_list"}, {Name: "users_delete"}}}
	r := NewRouter([]Client{rem, bed}, true)
	if len(r.ListTools()) != 3 {
		t.Fatalf("unexpected tools %+v", r.ListTools())
	}
	ctx := context.Background()
	r.CallTool(ctx, "bedolaga_user_get", map[string]any{"telegramId": 999, "user_id": 55, "telegram_id": 999, "other": "x"}, 42)
	a := bed.args[0]
	if len(a) != 2 || a["telegram_id"] != int64(42) || a["other"] != "x" {
		t.Fatalf("model identity leaked %v", a)
	}
	r.CallTool(ctx, "bedolaga_user_get", map[string]any{"telegram_id": 55, "user_id": 22}, -91)
	if bed.args[1]["user_id"] != int64(91) {
		t.Fatal(bed.args)
	}
	if _, ok := bed.args[1]["telegram_id"]; ok {
		t.Fatal("cabinet got fabricated Telegram id")
	}
	result, _ := r.CallTool(ctx, "nodes_list", nil, -91)
	if !contains(result, "identity_unavailable") {
		t.Fatal(result)
	}
	result, _ = r.CallTool(ctx, "bedolaga_user_get", nil, 0)
	if !contains(result, "identity_unavailable") {
		t.Fatal(result)
	}
	r.CallTool(ctx, "users_get_by_telegram_id", map[string]any{"telegramId": 999}, 42)
	if rem.args[0]["telegramId"] != int64(42) {
		t.Fatal(rem.args)
	}
	result, _ = r.CallTool(ctx, "hwid_device_delete", nil, 42)
	if !contains(result, "not allowed") {
		t.Fatal(result)
	}
}
func contains(s, q string) bool { return len(s) >= len(q) && stringsContains(s, q) }
func stringsContains(s, q string) bool {
	for i := 0; i+len(q) <= len(s); i++ {
		if s[i:i+len(q)] == q {
			return true
		}
	}
	return false
}
func TestRouterCollisionReconnectAndInternalOwnership(t *testing.T) {
	ctx := context.Background()
	rem := &mockClient{name: "remnawave", tools: []Tool{{Name: "users_get", InputSchema: schema(map[string]string{"userId": "string"})}, {Name: "users_get_by_telegram_id", InputSchema: schema(map[string]string{"telegramId": "number"})}}, results: map[string]string{"users_get_by_telegram_id": `{"response":{"users":[{"id":17,"uuid":"owner-uuid","telegramId":42}]}}`}}
	bed := &mockClient{name: "bedolaga", tools: []Tool{{Name: "users_get"}}}
	r := NewRouter([]Client{rem, bed}, false)
	for _, tool := range r.ListTools() {
		if tool.Name == "users_get" {
			t.Fatal("colliding tool exposed")
		}
	}
	bed.tools = nil
	if len(r.ListTools()) != 2 {
		t.Fatal("reconnected route cache stale")
	}
	result, _ := r.CallTool(ctx, "users_get", map[string]any{"userId": "foreign-uuid"}, 42)
	if !contains(result, "identity_unavailable") {
		t.Fatal(result)
	}
	if len(rem.calls) != 1 || rem.calls[0] != "users_get_by_telegram_id" {
		t.Fatal("foreign internal lookup reached upstream")
	}
	r.CallTool(ctx, "users_get", map[string]any{"userId": "owner-uuid"}, 42)
	if len(rem.calls) != 3 || rem.calls[2] != "users_get" {
		t.Fatal("legitimate owned user rejected")
	}
	if rem.args[1]["telegramId"] != int64(42) {
		t.Fatal("ownership lookup not pinned")
	}
}
func TestUnknownOwnerAndSchemaCoercion(t *testing.T) {
	unknown := &mockClient{name: "unknown", tools: []Tool{{Name: "nodes_list"}}}
	if len(NewRouter([]Client{unknown}, false).ListTools()) != 0 {
		t.Fatal("unknown owner exposed")
	}
	for _, c := range []struct {
		typ      string
		supplied any
		want     any
	}{{"string", 12, "42"}, {"number", "12", int64(42)}, {"integer", nil, int64(42)}, {"", float64(12), int64(42)}, {"", true, "42"}} {
		got := coerce(42, c.typ, c.supplied)
		a, _ := json.Marshal(got)
		b, _ := json.Marshal(c.want)
		if string(a) != string(b) {
			t.Fatalf("%v != %v", got, c.want)
		}
	}
}
