// Package diagnostics implements opt-in TRACE payload diagnostics. INFO/ERROR
// records contain operational metadata only; recursive sanitization never
// mutates application payloads or provider signature/reasoning state.
package diagnostics

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"reflect"
	"strings"
	"supportbot/internal/logging"
	"time"
)

const TraceLevel = slog.Level(-8)
const MaxPayloadCharacters = 20000

var credentials = logging.NewRedactor()

func Register(secret string) { credentials.Register(secret) }
func RegisterSettings(settings any) {
	v := reflect.ValueOf(settings)
	if v.Kind() == reflect.Pointer {
		v = v.Elem()
	}
	if v.Kind() != reflect.Struct {
		return
	}
	t := v.Type()
	for i := 0; i < v.NumField(); i++ {
		name := strings.ToLower(t.Field(i).Name)
		if v.Field(i).Kind() == reflect.String && (strings.Contains(name, "key") || strings.Contains(name, "token") || strings.Contains(name, "password") || strings.Contains(name, "secret")) {
			Register(v.Field(i).String())
		}
	}
}
func Enabled(ctx context.Context) bool { return slog.Default().Enabled(ctx, TraceLevel) }
func Sensitive(key string) bool {
	key = strings.ReplaceAll(strings.ToLower(key), "-", "_")
	normalized := strings.ReplaceAll(key, "_", "")
	switch normalized {
	case "prompttokens", "completiontokens", "totaltokens", "inputtokens", "outputtokens", "maxtokens", "maxoutputtokens", "maxcompletiontokens", "cachedtokens", "reasoningtokens":
		return false
	}
	for _, part := range []string{"password", "passwd", "secret", "token", "api_key", "apikey", "auth", "cookie", "private_key"} {
		if strings.Contains(key, part) {
			return true
		}
	}
	return false
}
func Redact(v any) any { return redactDepth(v, 0) }
func redactDepth(v any, depth int) any {
	if depth > 64 {
		return "[MAX DEPTH]"
	}
	switch x := v.(type) {
	case nil, bool, int, int64, float64, float32, json.Number:
		return x
	case string:
		trimmed := strings.TrimSpace(x)
		if strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "[") {
			var decoded any
			if json.Unmarshal([]byte(x), &decoded) == nil {
				b, _ := json.Marshal(redactDepth(decoded, depth+1))
				return string(b)
			}
		}
		if strings.HasPrefix(x, "data:") && strings.Contains(x, ";base64,") {
			return "[BINARY DATA OMITTED]"
		}
		return credentials.Text(x)
	case []byte:
		return redactDepth(string(x), depth+1)
	case map[string]any:
		out := map[string]any{}
		for k, value := range x {
			if Sensitive(k) {
				out[k] = "[REDACTED]"
			} else if k == "data" {
				_, snake := x["mime_type"]
				_, camel := x["mimeType"]
				if snake || camel {
					out[k] = "[BINARY DATA OMITTED]"
				} else {
					out[k] = redactDepth(value, depth+1)
				}
			} else {
				out[k] = redactDepth(value, depth+1)
			}
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, value := range x {
			out[i] = redactDepth(value, depth+1)
		}
		return out
	case error:
		return credentials.Text(x.Error())
	}
	// SDK structs/typed slices/maps are normalized before recursive redaction.
	b, err := json.Marshal(v)
	if err != nil {
		return "[UNSERIALIZABLE]"
	}
	var normalized any
	if json.Unmarshal(b, &normalized) != nil {
		return "[UNSERIALIZABLE]"
	}
	return redactDepth(normalized, depth+1)
}
func Payload(ctx context.Context, message string, v any, attrs ...any) {
	if !Enabled(ctx) {
		return
	}
	attrs = append(contextAttrs(ctx), attrs...)
	safe := Redact(v)
	encoded, err := json.Marshal(safe)
	if err == nil && len(encoded) > MaxPayloadCharacters {
		runes := []rune(string(encoded))
		if len(runes) > MaxPayloadCharacters {
			safe = string(runes[:MaxPayloadCharacters]) + " [TRUNCATED]"
		}
	}
	attrs = append(attrs, "payload", safe)
	slog.Log(ctx, TraceLevel, message, attrs...)
}
func Error(ctx context.Context, message string, err error, attrs ...any) {
	kind := "unknown"
	if err != nil {
		kind = fmt.Sprintf("%T", err)
	}
	attrs = append(attrs, "error_class", kind)
	slog.ErrorContext(ctx, message, append(contextAttrs(ctx), attrs...)...)
	if err != nil {
		Payload(ctx, message+" details", err, attrs...)
	}
}

type contextKey string

func NewID() string {
	var raw [8]byte
	if _, err := rand.Read(raw[:]); err == nil {
		return hex.EncodeToString(raw[:])
	}
	return fmt.Sprintf("%x", time.Now().UnixNano())
}
func WithCorrelation(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, contextKey("correlation_id"), id)
}
func WithRequest(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, contextKey("request_id"), id)
}
func WithOperation(ctx context.Context, component, operation string) context.Context {
	ctx = context.WithValue(ctx, contextKey("component"), component)
	return context.WithValue(ctx, contextKey("operation"), operation)
}
func WithAttempt(ctx context.Context, attempt int) context.Context {
	return context.WithValue(ctx, contextKey("attempt_id"), attempt)
}
func CorrelationID(ctx context.Context) string {
	id, _ := ctx.Value(contextKey("correlation_id")).(string)
	return id
}
func contextAttrs(ctx context.Context) []any {
	out := []any{}
	for _, key := range []contextKey{"correlation_id", "request_id", "component", "operation", "attempt_id"} {
		if v := ctx.Value(key); v != nil {
			out = append(out, string(key), v)
		}
	}
	return out
}
func Info(ctx context.Context, message string, attrs ...any) {
	attrs = append(contextAttrs(ctx), attrs...)
	slog.InfoContext(ctx, message, attrs...)
}
