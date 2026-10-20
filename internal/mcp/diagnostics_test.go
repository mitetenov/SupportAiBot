package mcp

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"supportbot/internal/diagnostics"
	"testing"
)

func TestRouterDiagnosticsOptInAndRedaction(t *testing.T) {
	for _, level := range []slog.Level{slog.LevelInfo, slog.LevelError, diagnostics.TraceLevel} {
		t.Run(level.String(), func(t *testing.T) {
			old := slog.Default()
			out := &bytes.Buffer{}
			slog.SetDefault(slog.New(slog.NewJSONHandler(out, &slog.HandlerOptions{Level: level})))
			defer slog.SetDefault(old)
			client := &mockClient{name: "remnawave", tools: []Tool{{Name: "nodes_list"}}, results: map[string]string{"nodes_list": `{"node":"PRIVATE_NODE_DETAILS","secret":"PRIVATE_MCP_TOKEN"}`}}
			router := NewRouter([]Client{client}, false)
			result, err := router.CallTool(context.Background(), "nodes_list", map[string]any{"filter": "PRIVATE_TOOL_ARGUMENT", "api_key": "PRIVATE_ARG_TOKEN"}, 42)
			if err != nil || !strings.Contains(result, "PRIVATE_NODE_DETAILS") {
				t.Fatal("diagnostics changed tool result")
			}
			logs := out.String()
			for _, secret := range []string{"PRIVATE_MCP_TOKEN", "PRIVATE_ARG_TOKEN"} {
				if strings.Contains(logs, secret) {
					t.Fatalf("credential escaped redaction at %s", level)
				}
			}
			for _, private := range []string{"PRIVATE_NODE_DETAILS", "PRIVATE_TOOL_ARGUMENT"} {
				if strings.Contains(logs, private) != (level == diagnostics.TraceLevel) {
					t.Fatal("MCP raw payload opt-in mismatch")
				}
			}
			if level != slog.LevelError {
				for _, meta := range []string{"MCP route", "server", "tool", "duration_seconds", "outcome"} {
					if !strings.Contains(logs, meta) {
						t.Fatalf("route operational event missing %s", meta)
					}
				}
			}
		})
	}
}
