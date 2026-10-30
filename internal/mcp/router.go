// Package mcp isolates server ownership and trusted caller identity from model input.
package mcp

import (
	"context"
	"encoding/json"
	"io"
	"math"
	"sort"
	"strconv"
	"strings"
	"supportbot/internal/diagnostics"
	"sync"
	"time"
)

type Tool struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
}
type Client interface {
	ServerName() string
	ListTools() []Tool
	CallTool(context.Context, string, map[string]any) (string, error)
}
type route struct {
	client Client
	tool   Tool
	owner  string
}
type Router struct {
	clients    []Client
	readonly   bool
	mu         sync.RWMutex
	routes     map[string]route
	tools      []Tool
	collisions map[string]bool
}

var remReads = set("users_get_by_telegram_id", "users_get_subscription_url_by_telegram_id", "users_get", "subscriptions_get_by_user_id", "users_accessible_nodes", "bandwidth_user_usage", "hwid_devices_list", "nodes_list", "nodes_get")
var bedReads = set("bedolaga_user_get", "bedolaga_billing_get", "bedolaga_referrals_get", "bedolaga_subscription_get", "bedolaga_tickets_get", "bedolaga_payment_status_get", "bedolaga_promocode_check", "bedolaga_gifts_get")

func set(names ...string) map[string]bool {
	m := map[string]bool{}
	for _, n := range names {
		m[n] = true
	}
	return m
}
func NewRouter(clients []Client, readonly bool) *Router {
	r := &Router{clients: clients, readonly: readonly}
	r.refresh()
	return r
}
func (r *Router) refresh() {
	r.mu.Lock()
	defer r.mu.Unlock()
	owners := map[string]map[string]bool{}
	snapshots := make([][]Tool, len(r.clients))
	for i, c := range r.clients {
		snapshots[i] = c.ListTools()
		for _, t := range snapshots[i] {
			if owners[t.Name] == nil {
				owners[t.Name] = map[string]bool{}
			}
			owners[t.Name][c.ServerName()] = true
		}
	}
	r.routes = map[string]route{}
	r.tools = nil
	r.collisions = map[string]bool{}
	for n, o := range owners {
		if len(o) > 1 {
			r.collisions[n] = true
		}
	}
	for i, c := range r.clients {
		for _, t := range snapshots[i] {
			o := c.ServerName()
			allowed := (o == "remnawave" && (remReads[t.Name] || (!r.readonly && t.Name == "hwid_device_delete"))) || (o == "bedolaga" && bedReads[t.Name])
			if allowed && !r.collisions[t.Name] {
				r.routes[t.Name] = route{c, t, o}
				r.tools = append(r.tools, t)
			}
		}
	}
}
func (r *Router) ListTools() []Tool {
	r.refresh()
	r.mu.RLock()
	defer r.mu.RUnlock()
	return append([]Tool(nil), r.tools...)
}
func jsonString(v any) string { b, _ := json.Marshal(v); return string(b) }
func identityUnavailable(name string) string {
	return jsonString(map[string]any{"ok": false, "source": "mcp", "tool": name, "error": map[string]any{"code": "identity_unavailable", "message": "Идентификация пользователя недоступна: у этого разговора нет Telegram-аккаунта.", "retryable": false}})
}

func ownershipError(name, code string) string {
	message := "Не удалось подтвердить принадлежность выбранного аккаунта текущему пользователю. Заново получите список устройств и используйте userId/hwid из него."
	retryable := code == "ownership_check_unavailable"
	if retryable {
		message = "Сервис проверки владельца аккаунта временно недоступен. Повторите проверку позже; это не означает отсутствия Telegram-аккаунта."
	}
	return jsonString(map[string]any{"ok": false, "source": "mcp", "tool": name, "error": map[string]any{"code": code, "message": message, "retryable": retryable}})
}
func identityName(s string) string { return strings.ToLower(strings.ReplaceAll(s, "_", "")) }
func property(t Tool, kind string) (string, string) {
	p, _ := t.InputSchema["properties"].(map[string]any)
	for k, v := range p {
		if identityName(k) == kind {
			m, _ := v.(map[string]any)
			typ, _ := m["type"].(string)
			return k, typ
		}
	}
	return "", ""
}
func coerce(id int64, typ string, supplied any) any {
	switch typ {
	case "number", "integer":
		return id
	case "string":
		return strconv.FormatInt(id, 10)
	case "":
		switch supplied.(type) {
		case float64, float32, int, int64, json.Number:
			return id
		}
		return strconv.FormatInt(id, 10)
	default:
		return strconv.FormatInt(id, 10)
	}
}
func (r *Router) CallTool(ctx context.Context, name string, args map[string]any, key int64) (string, error) {
	r.refresh()
	if key == 0 {
		diagnostics.Error(ctx, "MCP call blocked", nil, "reason", "identity_unavailable")
		diagnostics.Payload(ctx, "MCP blocked call", name)
		return identityUnavailable(name), nil
	}
	r.mu.RLock()
	rt, ok := r.routes[name]
	collision := r.collisions[name]
	r.mu.RUnlock()
	if !ok {
		reason := "tool_not_allowed"
		if collision {
			reason = "tool_collision"
		}
		diagnostics.Error(ctx, "MCP call blocked", nil, "reason", reason)
		diagnostics.Payload(ctx, "MCP blocked tool", name)
		if collision {
			return jsonString(map[string]any{"error": "Tool hidden due to name collision: " + name}), nil
		}
		return jsonString(map[string]any{"error": "Tool not allowed: " + name}), nil
	}
	if key < 0 && rt.owner != "bedolaga" {
		diagnostics.Error(ctx, "MCP call blocked", nil, "server", rt.owner, "tool", name, "reason", "identity_unavailable")
		return identityUnavailable(name), nil
	}
	safe := map[string]any{}
	for k, v := range args {
		safe[k] = v
	}
	tn, tt := property(rt.tool, "telegramid")
	if rt.owner == "bedolaga" {
		for k := range safe {
			n := identityName(k)
			if n == "telegramid" || n == "userid" {
				delete(safe, k)
			}
		}
		if key < 0 {
			un, ut := property(rt.tool, "userid")
			if un == "" {
				un, ut = "user_id", "integer"
			}
			safe[un] = coerce(-key, ut, nil)
		} else {
			if tn == "" {
				tn, tt = "telegram_id", "integer"
			}
			safe[tn] = coerce(key, tt, nil)
		}
	} else {
		for k, v := range safe {
			if identityName(k) == "telegramid" {
				typ := ""
				if k == tn {
					typ = tt
				}
				safe[k] = coerce(key, typ, v)
			}
		}
		if tn != "" {
			if _, exists := safe[tn]; !exists {
				safe[tn] = coerce(key, tt, nil)
			}
		}
		// Narrow safety deviation from Python: every internal user ID is proved by
		// a fresh system-pinned Telegram lookup, including reads and HWID deletion.
		if un, _ := property(rt.tool, "userid"); un != "" {
			if code := r.checkOwnership(ctx, rt.client, safe[un], key); code != "" {
				diagnostics.Error(ctx, "MCP call blocked", nil, "server", rt.owner, "tool", name, "reason", code)
				return ownershipError(name, code), nil
			}
		}
	}
	diagnostics.Info(ctx, "MCP route", "server", rt.owner, "tool", name)
	diagnostics.Payload(ctx, "MCP pinned tool arguments", safe, "server", rt.owner, "tool", name, "telegram_id", key)
	started := time.Now()
	result, err := rt.client.CallTool(ctx, name, safe)
	outcome := "success"
	if err != nil {
		outcome = "error"
		diagnostics.Error(ctx, "MCP routed tool failed", err, "server", rt.owner, "tool", name)
	}
	diagnostics.Info(ctx, "MCP routed call", "server", rt.owner, "tool", name, "outcome", outcome, "duration_seconds", time.Since(started).Seconds())
	diagnostics.Payload(ctx, "MCP routed tool result", result, "server", rt.owner, "tool", name)
	return result, err
}
func (r *Router) checkOwnership(ctx context.Context, c Client, wanted any, key int64) string {
	wantedID, valid := identifier(wanted)
	if !valid {
		return "ownership_unverified"
	}
	result, err := c.CallTool(ctx, "users_get_by_telegram_id", map[string]any{"telegramId": key})
	if err != nil {
		return "ownership_check_unavailable"
	}
	var data any
	decoder := json.NewDecoder(strings.NewReader(result))
	decoder.UseNumber()
	if decoder.Decode(&data) != nil {
		return "ownership_check_unavailable"
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		return "ownership_check_unavailable"
	}
	if !validUserLookup(data) {
		return "ownership_check_unavailable"
	}
	if !containsUser(data, wantedID, key) {
		return "ownership_unverified"
	}
	return ""
}

func validUserLookup(value any) bool {
	switch x := value.(type) {
	case []any:
		for _, item := range x {
			if !validUserLookup(item) {
				return false
			}
		}
		return true
	case map[string]any:
		if ok, exists := x["ok"].(bool); exists && !ok || x["error"] != nil {
			return false
		}
		for _, field := range []string{"id", "userId", "uuid"} {
			if _, ok := identifier(x[field]); ok {
				return true
			}
		}
		for _, field := range []string{"response", "users", "data", "items", "results"} {
			if nested, exists := x[field]; exists && validUserLookup(nested) {
				return true
			}
		}
	}
	return false
}

// identifier keeps integer IDs in decimal form without rounding lookup results
// or accepting imprecise/fractional model arguments. UUID strings remain opaque.
func identifier(value any) (string, bool) {
	switch id := value.(type) {
	case string:
		return id, id != ""
	case json.Number:
		n, err := id.Int64()
		return strconv.FormatInt(n, 10), err == nil
	case int:
		return strconv.Itoa(id), true
	case int64:
		return strconv.FormatInt(id, 10), true
	case float64:
		if math.Abs(id) >= 1<<53 || math.Trunc(id) != id {
			return "", false
		}
		return strconv.FormatFloat(id, 'f', 0, 64), true
	default:
		return "", false
	}
}
func containsUser(v any, wanted string, key int64) bool {
	switch x := v.(type) {
	case []any:
		for _, a := range x {
			if containsUser(a, wanted, key) {
				return true
			}
		}
	case map[string]any:
		if e, ok := x["ok"].(bool); ok && !e {
			return false
		}
		for _, field := range []string{"id", "userId", "uuid"} {
			if id, ok := identifier(x[field]); ok && id == wanted {
				if raw, has := x["telegramId"]; has {
					tg, valid := identifier(raw)
					if !valid || tg != strconv.FormatInt(key, 10) {
						continue
					}
				}
				return true
			}
		}
		for _, field := range []string{"response", "users", "data", "items", "results"} {
			if containsUser(x[field], wanted, key) {
				return true
			}
		}
	}
	return false
}

// Collisions reports all servers declaring the same name, including hidden tools.
func (r *Router) Collisions() map[string][]string {
	owners := map[string]map[string]bool{}
	for _, c := range r.clients {
		for _, t := range c.ListTools() {
			if owners[t.Name] == nil {
				owners[t.Name] = map[string]bool{}
			}
			owners[t.Name][c.ServerName()] = true
		}
	}
	out := map[string][]string{}
	for name, byOwner := range owners {
		if len(byOwner) > 1 {
			for owner := range byOwner {
				out[name] = append(out[name], owner)
			}
			sort.Strings(out[name])
		}
	}
	return out
}
func (r *Router) ToolCount(serverName string) int {
	r.refresh()
	r.mu.RLock()
	defer r.mu.RUnlock()
	count := 0
	for _, rt := range r.routes {
		if rt.owner == serverName {
			count++
		}
	}
	return count
}
