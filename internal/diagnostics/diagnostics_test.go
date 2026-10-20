package diagnostics

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"reflect"
	"strings"
	"sync"
	"testing"
)

func capture(t *testing.T, level slog.Level) *bytes.Buffer {
	t.Helper()
	old := slog.Default()
	out := &bytes.Buffer{}
	slog.SetDefault(slog.New(slog.NewJSONHandler(out, &slog.HandlerOptions{Level: level})))
	t.Cleanup(func() { slog.SetDefault(old) })
	return out
}

type countedJSON struct{ calls *int }

func (c countedJSON) MarshalJSON() ([]byte, error) { *c.calls++; return []byte(`{"safe":true}`), nil }
func TestTraceRequiresExplicitLevelAndAvoidsSerialization(t *testing.T) {
	for _, level := range []slog.Level{slog.LevelInfo, slog.LevelError} {
		t.Run(level.String(), func(t *testing.T) {
			out := capture(t, level)
			calls := 0
			Payload(context.Background(), "private payload", countedJSON{&calls})
			if calls != 0 || out.Len() != 0 {
				t.Fatal("TRACE serialized or emitted at default level")
			}
		})
	}
	t.Run("TRACE", func(t *testing.T) {
		out := capture(t, TraceLevel)
		calls := 0
		Payload(context.Background(), "private payload", countedJSON{&calls})
		if calls != 1 || !strings.Contains(out.String(), "private payload") {
			t.Fatal("explicit TRACE did not emit payload")
		}
	})
}
func TestRecursiveRedactionAndInputImmutability(t *testing.T) {
	Register("CUSTOM_PROVIDER_KEY_SENTINEL")
	input := map[string]any{"text": "user question and CUSTOM_PROVIDER_KEY_SENTINEL", "headers": map[string]any{"Authorization": "Bearer unknown-jwt", "Cookie": "session=hidden"}, "response": `{"client_secret":"hidden-value","safe":"visible"}`, "input_image": "data:image/png;base64,PRIVATE_IMAGE_DATA", "parts": []any{map[string]any{"inline_data": map[string]any{"mime_type": "image/png", "data": "PRIVATE_GEMINI_DATA"}}}, "thoughtSignature": "opaque-signature", "user_id": float64(42)}
	before, _ := json.Marshal(input)
	result := Redact(input)
	after, _ := json.Marshal(input)
	if !bytes.Equal(before, after) {
		t.Fatal("diagnostics mutated wire request")
	}
	raw, _ := json.Marshal(result)
	for _, secret := range []string{"CUSTOM_PROVIDER_KEY_SENTINEL", "unknown-jwt", "session=hidden", "hidden-value", "PRIVATE_IMAGE_DATA", "PRIVATE_GEMINI_DATA"} {
		if strings.Contains(string(raw), secret) {
			t.Fatalf("credential/attachment escaped redaction: %s", secret)
		}
	}
	for _, public := range []string{"user question", "visible", "opaque-signature", "user_id"} {
		if !strings.Contains(string(raw), public) {
			t.Fatalf("non-secret diagnostic lost %s", public)
		}
	}
	if !reflect.DeepEqual(input["thoughtSignature"], "opaque-signature") {
		t.Fatal("signature changed")
	}
}
func TestErrorMetadataExcludesRemoteTextAtDefaultLevels(t *testing.T) {
	for _, level := range []slog.Level{slog.LevelInfo, slog.LevelError, TraceLevel} {
		t.Run(level.String(), func(t *testing.T) {
			out := capture(t, level)
			Error(context.Background(), "provider failed", errors.New("private provider body"), "provider", "fixture", "status_code", 400)
			s := out.String()
			if !strings.Contains(s, "error_class") || !strings.Contains(s, "status_code") {
				t.Fatal("safe error metadata missing")
			}
			if strings.Contains(s, "private provider body") != (level == TraceLevel) {
				t.Fatal("private exception text level mismatch")
			}
		})
	}
}
func TestCorrelationContextAndPayloadLimit(t *testing.T) {
	out := capture(t, TraceLevel)
	ctx := WithCorrelation(context.Background(), "corr-test")
	ctx = WithRequest(ctx, "req-test")
	ctx = WithOperation(ctx, "llm", "request")
	ctx = WithAttempt(ctx, 2)
	Info(ctx, "operational")
	Register("CAP_SECRET_SENTINEL")
	Payload(ctx, "long", map[string]any{"text": strings.Repeat("я", MaxPayloadCharacters+10), "secret": "CAP_SECRET_SENTINEL"})
	logs := out.String()
	for _, wanted := range []string{"corr-test", "req-test", "component", "attempt", "TRUNCATED"} {
		if !strings.Contains(logs, wanted) {
			t.Fatalf("diagnostic field missing %s", wanted)
		}
	}
	if strings.Contains(logs, "CAP_SECRET_SENTINEL") {
		t.Fatal("payload cap occurred before redaction")
	}
	if len(NewID()) < 8 {
		t.Fatal("correlation IDs too short")
	}
}
func TestUsageCountersAndCamelCaseMedia(t *testing.T) {
	input := map[string]any{"prompt_tokens": 123, "completion_tokens": 45, "TotalTokens": 168, "access_token": "credential", "inlineData": map[string]any{"mimeType": "image/png", "data": "PRIVATE_CAMEL_IMAGE"}}
	out := Redact(input).(map[string]any)
	if out["prompt_tokens"] != 123 || out["completion_tokens"] != 45 || out["TotalTokens"] != 168 {
		t.Fatal("usage counters censored")
	}
	raw, _ := json.Marshal(out)
	if strings.Contains(string(raw), "credential") || strings.Contains(string(raw), "PRIVATE_CAMEL_IMAGE") {
		t.Fatal("camel media or token escaped redaction")
	}
}
func TestCorrelationIsolatedAcrossConcurrentRequests(t *testing.T) {
	out := capture(t, slog.LevelInfo)
	var wg sync.WaitGroup
	for i := 0; i < 30; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			id := fmt.Sprint(i)
			ctx := WithCorrelation(context.Background(), "corr-"+id)
			ctx = WithRequest(ctx, "req-"+id)
			Info(ctx, "request", "index", id)
		}(i)
	}
	wg.Wait()
	for _, line := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		var row map[string]any
		if json.Unmarshal([]byte(line), &row) != nil {
			t.Fatal("invalid log JSON")
		}
		id := row["index"].(string)
		if row["correlation_id"] != "corr-"+id || row["request_id"] != "req-"+id {
			t.Fatal("request context leaked across goroutines")
		}
	}
}
func TestCyclicPayloadCannotBreakDiagnostics(t *testing.T) {
	m := map[string]any{}
	m["self"] = m
	result := Redact(m)
	raw, err := json.Marshal(result)
	if err != nil || !strings.Contains(string(raw), "MAX DEPTH") {
		t.Fatal("cyclic payload not bounded")
	}
}
