package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	sdk "github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestRenderResultPrecedence(t *testing.T) {
	for _, tc := range []struct {
		result *sdk.CallToolResult
		want   string
	}{{&sdk.CallToolResult{IsError: true, StructuredContent: map[string]any{"ok": true}, Content: []sdk.Content{&sdk.TextContent{Text: "bad"}}}, `{"error":"bad"}`}, {&sdk.CallToolResult{StructuredContent: map[string]any{"ok": true}, Content: []sdk.Content{&sdk.TextContent{Text: "ignored"}}}, `{"ok":true}`}, {&sdk.CallToolResult{StructuredContent: false}, `false`}, {&sdk.CallToolResult{Content: []sdk.Content{&sdk.TextContent{Text: "verbatim"}}}, `verbatim`}, {&sdk.CallToolResult{}, `[]`}} {
		if got := RenderResult(tc.result); got != tc.want {
			t.Fatalf("got %q want %q", got, tc.want)
		}
	}
}
func TestNativeSDKClientToolDiscoveryAndSessionLifetime(t *testing.T) {
	server := sdk.NewServer(&sdk.Implementation{Name: "fixture", Version: "1"}, &sdk.ServerOptions{PageSize: 1})
	server.AddTool(&sdk.Tool{Name: "one", InputSchema: map[string]any{"type": "object"}}, func(ctx context.Context, r *sdk.CallToolRequest) (*sdk.CallToolResult, error) {
		var args map[string]any
		json.Unmarshal(r.Params.Arguments, &args)
		return &sdk.CallToolResult{StructuredContent: args}, nil
	})
	server.AddTool(&sdk.Tool{Name: "two", InputSchema: map[string]any{"type": "object"}}, func(ctx context.Context, r *sdk.CallToolRequest) (*sdk.CallToolResult, error) {
		return &sdk.CallToolResult{Content: []sdk.Content{&sdk.TextContent{Text: "two"}}}, nil
	})
	handler := sdk.NewStreamableHTTPHandler(func(*http.Request) *sdk.Server { return server }, &sdk.StreamableHTTPOptions{JSONResponse: true, DisableLocalhostProtection: true})
	httpServer := httptest.NewServer(handler)
	defer httpServer.Close()
	c := NewHTTPClient("remnawave", httpServer.URL, httpServer.Client(), nil)
	ctx, cancel := context.WithCancel(context.Background())
	if !c.Init(ctx) {
		t.Fatal("MCP init failed")
	}
	defer c.Close()
	cancel()
	if len(c.ListTools()) != 2 || c.ProtocolVersion() == "" {
		t.Fatalf("discovery failed %+v", c.ListTools())
	}
	result, err := c.CallTool(context.Background(), "one", map[string]any{"telegramId": 42})
	if err != nil || !contains(result, "42") {
		t.Fatalf("session died with init context: %s %v", result, err)
	}
	c.Close()
	if c.Initialized() || len(c.ListTools()) != 0 {
		t.Fatal("closed client still initialized")
	}
}
func TestWriteTransportUnknownOutcomeIsNeverRetried(t *testing.T) {
	var calls atomic.Int32
	server := sdk.NewServer(&sdk.Implementation{Name: "fixture", Version: "1"}, nil)
	server.AddTool(&sdk.Tool{Name: "hwid_device_delete", InputSchema: map[string]any{"type": "object"}}, func(context.Context, *sdk.CallToolRequest) (*sdk.CallToolResult, error) {
		calls.Add(1)
		return &sdk.CallToolResult{Content: []sdk.Content{&sdk.TextContent{Text: "deleted"}}}, nil
	})
	handler := sdk.NewStreamableHTTPHandler(func(*http.Request) *sdk.Server { return server }, &sdk.StreamableHTTPOptions{JSONResponse: true, DisableLocalhostProtection: true})
	httpServer := httptest.NewServer(handler)
	defer httpServer.Close()
	httpClient := httpServer.Client()
	httpClient.Transport = &failureTransport{base: httpClient.Transport}
	c := NewHTTPClient("remnawave", httpServer.URL, httpClient, nil)
	if !c.Init(context.Background()) {
		t.Fatal("init failed")
	}
	defer c.Close()
	_, err := c.CallTool(context.Background(), "hwid_device_delete", map[string]any{})
	if err == nil {
		t.Fatal("unknown outcome returned as ordinary tool result")
	}
	if calls.Load() != 1 {
		t.Fatalf("write replayed %d times", calls.Load())
	}
}

type failureTransport struct{ base http.RoundTripper }

func (f *failureTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	raw := []byte{}
	if req.Body != nil {
		raw, _ = io.ReadAll(req.Body)
		req.Body = io.NopCloser(bytes.NewReader(raw))
	}
	res, err := f.base.RoundTrip(req)
	if (req.Header.Get("Mcp-Method") == "tools/call" || strings.Contains(string(raw), `"method":"tools/call"`)) && err == nil {
		res.Body.Close()
		return nil, errors.New("connection reset after upstream executed")
	}
	return res, err
}
func TestReadTransportReconnectsOnceAndRediscovers(t *testing.T) {
	var calls atomic.Int32
	server := sdk.NewServer(&sdk.Implementation{Name: "fixture", Version: "1"}, nil)
	server.AddTool(&sdk.Tool{Name: "nodes_list", InputSchema: map[string]any{"type": "object"}}, func(context.Context, *sdk.CallToolRequest) (*sdk.CallToolResult, error) {
		calls.Add(1)
		return &sdk.CallToolResult{Content: []sdk.Content{&sdk.TextContent{Text: "nodes"}}}, nil
	})
	handler := sdk.NewStreamableHTTPHandler(func(*http.Request) *sdk.Server { return server }, &sdk.StreamableHTTPOptions{JSONResponse: true, DisableLocalhostProtection: true})
	httpServer := httptest.NewServer(handler)
	defer httpServer.Close()
	client := httpServer.Client()
	client.Transport = &failOnceTransport{base: client.Transport}
	c := NewHTTPClient("remnawave", httpServer.URL, client, nil)
	if !c.Init(context.Background()) {
		t.Fatal("init failed")
	}
	defer c.Close()
	result, err := c.CallTool(context.Background(), "nodes_list", nil)
	if err != nil || result != "nodes" || calls.Load() != 2 {
		t.Fatalf("recovery failed result=%q err=%v calls=%d", result, err, calls.Load())
	}
	if !c.Initialized() || len(c.ListTools()) != 1 {
		t.Fatal("recovery lost tool cache")
	}
}

type failOnceTransport struct {
	base   http.RoundTripper
	failed atomic.Bool
}

func (f *failOnceTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	raw := []byte{}
	if req.Body != nil {
		raw, _ = io.ReadAll(req.Body)
		req.Body = io.NopCloser(bytes.NewReader(raw))
	}
	res, err := f.base.RoundTrip(req)
	if (req.Header.Get("Mcp-Method") == "tools/call" || strings.Contains(string(raw), `"method":"tools/call"`)) && err == nil && !f.failed.Swap(true) {
		res.Body.Close()
		return nil, errors.New("connection reset after response")
	}
	return res, err
}
func TestLegacyHandshakeFallback(t *testing.T) {
	server := sdk.NewServer(&sdk.Implementation{Name: "legacy-fixture", Version: "1"}, nil)
	server.AddTool(&sdk.Tool{Name: "nodes_list", InputSchema: map[string]any{"type": "object"}}, func(context.Context, *sdk.CallToolRequest) (*sdk.CallToolResult, error) {
		return &sdk.CallToolResult{Content: []sdk.Content{&sdk.TextContent{Text: "legacy nodes"}}}, nil
	})
	handler := sdk.NewStreamableHTTPHandler(func(*http.Request) *sdk.Server { return server }, &sdk.StreamableHTTPOptions{JSONResponse: true, DisableLocalhostProtection: true})
	var initialized atomic.Bool
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw := []byte{}
		if r.Body != nil {
			raw, _ = io.ReadAll(r.Body)
			r.Body = io.NopCloser(bytes.NewReader(raw))
		}
		var rpc map[string]any
		json.Unmarshal(raw, &rpc)
		method, _ := rpc["method"].(string)
		if method == "" {
			method = r.Header.Get("Mcp-Method")
		}
		if method == "server/discover" {
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": rpc["id"], "error": map[string]any{"code": -32601, "message": "Method not found"}})
			return
		}
		if method == "initialize" {
			initialized.Store(true)
		}
		handler.ServeHTTP(w, r)
	}))
	defer fixture.Close()
	client := NewHTTPClient("remnawave", fixture.URL, fixture.Client(), nil)
	if !client.Init(context.Background()) {
		t.Fatal("legacy fallback failed")
	}
	defer client.Close()
	if !initialized.Load() || client.ProtocolVersion() != "2025-11-25" {
		t.Fatalf("wrong legacy initialization %v %q", initialized.Load(), client.ProtocolVersion())
	}
	result, err := client.CallTool(context.Background(), "nodes_list", nil)
	if err != nil || result != "legacy nodes" {
		t.Fatalf("legacy tools call failed %q %v", result, err)
	}
}
