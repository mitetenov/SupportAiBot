package rag

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"supportbot/internal/config"
	"supportbot/internal/diagnostics"
	"testing"
)

func TestEmbeddingDiagnosticsMetadataAndOptInTrace(t *testing.T) {
	original := slog.Default()
	defer slog.SetDefault(original)
	for _, trace := range []bool{false, true} {
		t.Run(map[bool]string{false: "INFO", true: "TRACE"}[trace], func(t *testing.T) {
			var output bytes.Buffer
			level := slog.LevelInfo
			if trace {
				level = diagnostics.TraceLevel
			}
			slog.SetDefault(slog.New(slog.NewJSONHandler(&output, &slog.HandlerOptions{Level: level})))
			settings := config.Default()
			settings.EmbeddingProvider = "openai"
			settings.OpenAIAPIKey = "sk-local-secret-long-value"
			client := &http.Client{Transport: embeddingTransport(func(r *http.Request) (*http.Response, error) {
				return &http.Response{StatusCode: 200, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(`{"data":[{"embedding":[1,0,0]}]}`))}, nil
			})}
			provider := NewEmbedding(settings, client)
			ctx := diagnostics.WithCorrelation(context.Background(), "embedding-correlation")
			if _, e := provider.Embed(ctx, "private-query-sentinel"); e != nil {
				t.Fatal(e)
			}
			logs := output.String()
			if !strings.Contains(logs, "Embedding HTTP attempt") || !strings.Contains(logs, "embedding-correlation") {
				t.Fatal("operational metadata/correlation absent", logs)
			}
			if strings.Contains(logs, settings.OpenAIAPIKey) {
				t.Fatal("credential exposed", logs)
			}
			if strings.Contains(logs, "private-query-sentinel") != trace {
				t.Fatal("TRACE opt-in contract violated", logs)
			}
		})
	}
}
