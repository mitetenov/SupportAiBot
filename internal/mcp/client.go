package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"supportbot/internal/diagnostics"
	"sync"
	"time"

	sdk "github.com/modelcontextprotocol/go-sdk/mcp"
)

type NotifyFunc func(context.Context, string, error)
type HTTPClient struct {
	Name, BaseURL string
	HTTP          *http.Client
	Notify        NotifyFunc
	mu            sync.RWMutex
	recovery      sync.Mutex
	session       *sdk.ClientSession
	cancel        context.CancelFunc
	tools         []Tool
	generation    uint64
	closed        bool
}

func NewHTTPClient(name, baseURL string, httpClient *http.Client, notify NotifyFunc) *HTTPClient {
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 30 * time.Second}
	} else {
		clone := *httpClient
		clone.Timeout = 30 * time.Second
		httpClient = &clone
	}
	return &HTTPClient{Name: name, BaseURL: baseURL, HTTP: httpClient, Notify: notify}
}
func (c *HTTPClient) ServerName() string { return c.Name }
func (c *HTTPClient) ListTools() []Tool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return append([]Tool(nil), c.tools...)
}
func (c *HTTPClient) Initialized() bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.session != nil && !c.closed
}
func (c *HTTPClient) ProtocolVersion() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.session == nil {
		return ""
	}
	return c.session.InitializeResult().ProtocolVersion
}
func (c *HTTPClient) Init(ctx context.Context) bool {
	c.recovery.Lock()
	defer c.recovery.Unlock()
	return c.connect(ctx, true)
}
func (c *HTTPClient) connect(ctx context.Context, notify bool) bool {
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return false
	}
	old, oldCancel := c.session, c.cancel
	c.session = nil
	c.cancel = nil
	c.tools = nil
	c.mu.Unlock()
	if old != nil {
		old.Close()
	}
	if oldCancel != nil {
		oldCancel()
	}
	sessionCtx, cancel := context.WithCancel(context.WithoutCancel(ctx))
	endpoint := c.BaseURL
	client := sdk.NewClient(&sdk.Implementation{Name: "vpn-support-bot", Version: "2.0.1"}, nil)
	session, err := client.Connect(sessionCtx, &sdk.StreamableClientTransport{Endpoint: endpoint, HTTPClient: c.HTTP, DisableStandaloneSSE: true, MaxRetries: -1}, nil)
	if err != nil {
		cancel()
		c.report(ctx, notify, "init failed", err)
		return false
	}
	tools := []Tool{}
	cursor := ""
	seen := map[string]bool{}
	for {
		listCtx, listCancel := context.WithTimeout(ctx, 30*time.Second)
		result, e := session.ListTools(listCtx, &sdk.ListToolsParams{Cursor: cursor})
		listCancel()
		if e != nil {
			session.Close()
			cancel()
			c.report(ctx, notify, "list_tools failed", e)
			return false
		}
		for _, t := range result.Tools {
			schema := map[string]any{}
			encoded, _ := json.Marshal(t.InputSchema)
			json.Unmarshal(encoded, &schema)
			tools = append(tools, Tool{t.Name, t.Description, schema})
		}
		cursor = result.NextCursor
		if cursor == "" {
			break
		}
		if seen[cursor] {
			session.Close()
			cancel()
			c.report(ctx, notify, "list_tools repeated cursor", errors.New("MCP repeated pagination cursor"))
			return false
		}
		seen[cursor] = true
	}
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		session.Close()
		cancel()
		return false
	}
	c.session = session
	c.cancel = cancel
	c.tools = tools
	c.generation++
	c.mu.Unlock()
	diagnostics.Info(ctx, "MCP initialized", "server", c.Name, "tools", len(tools), "protocol_version", session.InitializeResult().ProtocolVersion)
	diagnostics.Payload(ctx, "MCP tool schemas", tools, "server", c.Name)
	return true
}
func (c *HTTPClient) report(ctx context.Context, notify bool, operation string, err error) {
	diagnostics.Error(ctx, "MCP operation failed", err, "server", c.Name, "operation", operation)
	if notify && c.Notify != nil {
		c.Notify(ctx, fmt.Sprintf("MCP[%s] %s", c.Name, operation), err)
	}
}
func (c *HTTPClient) RetryConnection(ctx context.Context) {
	delay := 5 * time.Second
	for {
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
		if c.Initialized() {
			delay = 5 * time.Second
			continue
		}
		c.recovery.Lock()
		connected := c.connect(ctx, false)
		c.recovery.Unlock()
		if connected {
			delay = 5 * time.Second
		} else {
			delay *= 2
			if delay > 60*time.Second {
				delay = 60 * time.Second
			}
		}
	}
}
func (c *HTTPClient) CallTool(ctx context.Context, name string, args map[string]any) (string, error) {
	c.mu.RLock()
	session, generation, closed := c.session, c.generation, c.closed
	c.mu.RUnlock()
	if session == nil || closed {
		return jsonString(map[string]any{"error": fmt.Sprintf("MCP[%s] client not initialized", c.Name)}), nil
	}
	if args == nil {
		args = map[string]any{}
	}
	started := time.Now()
	diagnostics.Payload(ctx, "MCP tool request", args, "server", c.Name, "tool", name, "attempt", 1)
	result, err := session.CallTool(ctx, &sdk.CallToolParams{Name: name, Arguments: args})
	outcome := "success"
	if err != nil || result != nil && result.IsError {
		outcome = "error"
	}
	diagnostics.Info(ctx, "MCP call", "server", c.Name, "tool", name, "attempt", 1, "outcome", outcome, "duration_seconds", time.Since(started).Seconds())
	diagnostics.Payload(ctx, "MCP tool response", result, "server", c.Name, "tool", name, "attempt", 1)
	if err == nil {
		if result.IsError {
			c.report(ctx, true, "tool error: "+name, errors.New("MCP tool returned error"))
		}
		return RenderResult(result), nil
	}
	if ctx.Err() != nil {
		return "", ctx.Err()
	}
	if !recoverable(err) {
		c.report(ctx, true, "tool call failed: "+name, err)
		return jsonString(map[string]any{"error": err.Error()}), nil
	}
	// An interrupted call may have reached the server. Only replay read operations;
	// an unknown write outcome must surface to the LLM coordinator without failover.
	if name == "hwid_device_delete" {
		c.report(ctx, true, "write outcome unknown: "+name, err)
		return "", fmt.Errorf("MCP write %s outcome unknown: %w", name, err)
	}
	diagnostics.Info(ctx, "MCP reconnecting after transport failure", "server", c.Name, "tool", name)
	diagnostics.Payload(ctx, "MCP transport error", err, "server", c.Name, "tool", name)
	c.recovery.Lock()
	c.mu.RLock()
	same := c.generation == generation
	current := c.session
	c.mu.RUnlock()
	recovered := current != nil
	if same {
		recovered = c.connect(ctx, true)
	}
	c.recovery.Unlock()
	if !recovered {
		return jsonString(map[string]any{"error": fmt.Sprintf("MCP[%s] session lost and not recovered: %s", c.Name, name)}), nil
	}
	c.mu.RLock()
	session = c.session
	c.mu.RUnlock()
	if session == nil {
		return jsonString(map[string]any{"error": fmt.Sprintf("MCP[%s] client not initialized", c.Name)}), nil
	}
	started = time.Now()
	diagnostics.Payload(ctx, "MCP tool request", args, "server", c.Name, "tool", name, "attempt", 2)
	result, err = session.CallTool(ctx, &sdk.CallToolParams{Name: name, Arguments: args})
	outcome = "success"
	if err != nil || result != nil && result.IsError {
		outcome = "error"
	}
	diagnostics.Info(ctx, "MCP call", "server", c.Name, "tool", name, "attempt", 2, "outcome", outcome, "duration_seconds", time.Since(started).Seconds())
	diagnostics.Payload(ctx, "MCP tool response", result, "server", c.Name, "tool", name, "attempt", 2)
	if err != nil {
		c.report(ctx, true, "tool call failed after reconnect: "+name, err)
		return jsonString(map[string]any{"error": err.Error()}), nil
	}
	return RenderResult(result), nil
}
func (c *HTTPClient) Close() error {
	c.recovery.Lock()
	defer c.recovery.Unlock()
	c.mu.Lock()
	c.closed = true
	s, cancel := c.session, c.cancel
	c.session = nil
	c.cancel = nil
	c.tools = nil
	c.mu.Unlock()
	var err error
	if s != nil {
		err = s.Close()
	}
	if cancel != nil {
		cancel()
	}
	return err
}
func recoverable(err error) bool {
	if errors.Is(err, io.EOF) || errors.Is(err, net.ErrClosed) {
		return true
	}
	var neterr net.Error
	if errors.As(err, &neterr) {
		return true
	}
	s := strings.ToLower(err.Error())
	for _, p := range []string{"session not found", "session expired", "connection closed", "connection refused", "connection reset", "remote protocol error", "broken pipe", "server not initialized", "not initialized", "closed pipe"} {
		if strings.Contains(s, p) {
			return true
		}
	}
	return false
}
func RenderResult(result *sdk.CallToolResult) string {
	if result == nil {
		return "null"
	}
	if result.IsError {
		texts := []string{}
		for _, block := range result.Content {
			if t, ok := block.(*sdk.TextContent); ok && t.Text != "" {
				texts = append(texts, t.Text)
			}
		}
		message := "Tool execution failed"
		if len(texts) > 0 {
			message = strings.Join(texts, ", ")
		}
		return jsonString(map[string]any{"error": message})
	}
	if result.StructuredContent != nil {
		return jsonString(result.StructuredContent)
	}
	if len(result.Content) == 1 {
		if t, ok := result.Content[0].(*sdk.TextContent); ok {
			return t.Text
		}
	}
	if len(result.Content) == 0 {
		return "[]"
	}
	return jsonString(result.Content)
}
