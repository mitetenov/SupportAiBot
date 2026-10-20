package llm

import (
	"bytes"
	"context"
	"log/slog"
	"net/http"
	"strings"
	"supportbot/internal/diagnostics"
	"testing"
)

func TestProviderDiagnosticsLevelsAndRedaction(t *testing.T) {
	for _, level := range []slog.Level{slog.LevelInfo, slog.LevelError, diagnostics.TraceLevel} {
		t.Run(level.String(), func(t *testing.T) {
			old := slog.Default()
			out := &bytes.Buffer{}
			slog.SetDefault(slog.New(slog.NewJSONHandler(out, &slog.HandlerOptions{Level: level})))
			defer slog.SetDefault(old)
			p, err := NewProvider(ProviderConfig{Name: "zai", Model: "glm-5.3-flash", APIKey: "PRIVATE_CUSTOM_ZAI_KEY_SENTINEL", BaseURL: "https://fixture", ReasoningEffort: "none"}, nil, &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
				return httpJSON(400, `{"error":{"message":"PRIVATE_PROVIDER_BODY PRIVATE_CUSTOM_ZAI_KEY_SENTINEL","api_key":"PRIVATE_REMOTE_TOKEN"}}`), nil
			})})
			if err != nil {
				t.Fatal(err)
			}
			conversation := p.Initial("PRIVATE_USER_QUESTION", 42, "PRIVATE_FAQ_CONTEXT", "PRIVATE_BINARY_DATA", "image/png", nil)
			_, err = p.Call(context.Background(), conversation)
			if err == nil {
				t.Fatal("fixture error ignored")
			}
			logs := out.String()
			for _, secret := range []string{"PRIVATE_CUSTOM_ZAI_KEY_SENTINEL", "PRIVATE_REMOTE_TOKEN", "PRIVATE_BINARY_DATA"} {
				if strings.Contains(logs, secret) {
					t.Fatalf("secret escaped redaction at %s", level)
				}
			}
			if level == slog.LevelInfo || level == diagnostics.TraceLevel {
				for _, meta := range []string{"Selected LLM", "effective_effort", "LLM HTTP attempt", "duration_seconds", "attempt"} {
					if !strings.Contains(logs, meta) {
						t.Fatalf("operational diagnostic missing %s", meta)
					}
				}
			}
			if !strings.Contains(logs, "status_code") || !strings.Contains(logs, "error_class") {
				t.Fatal("provider error metadata missing")
			}
			for _, private := range []string{"PRIVATE_USER_QUESTION", "PRIVATE_FAQ_CONTEXT", "PRIVATE_PROVIDER_BODY"} {
				if strings.Contains(logs, private) != (level == diagnostics.TraceLevel) {
					t.Fatalf("raw opt-in level mismatch for %s at %s", private, level)
				}
			}
		})
	}
}
func TestEffectiveReasoningDiagnosticsMatchNativeParameters(t *testing.T) {
	for _, tc := range []struct{ provider, model, effort, want string }{{"openai", "gpt-6-luna", "none", "none"}, {"openai", "custom", "high", "unsupported/ignored"}, {"gemini", "gemini-3.5-flash-lite", "none", "minimal"}, {"gemini", "gemini-2.5-pro", "high", "budget=24576"}, {"deepseek", "deepseek-flash", "medium", "high"}, {"groq", "openai/gpt-oss-20b", "none", "low"}, {"zai", "glm-5.3-flash", "none", "low"}, {"openrouter", "z-ai/glm-4.7", "high", "enabled"}} {
		p, e := NewProvider(ProviderConfig{Name: tc.provider, Model: tc.model, ReasoningEffort: tc.effort, APIKey: "test-key"}, nil, nil)
		if e != nil {
			t.Fatal(e)
		}
		if got := p.EffectiveReasoningEffort(); got != tc.want {
			t.Fatalf("%s got %s want %s", tc.provider, got, tc.want)
		}
	}
}
