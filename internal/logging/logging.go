// Package logging provides process-wide credential redaction for standard and structured logs.
package logging

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"log/slog"
	"net/url"
	"os"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"supportbot/internal/config"
	"sync"
)

type Redactor struct {
	mu      sync.RWMutex
	secrets []string
}

func NewRedactor(secrets ...string) *Redactor {
	r := &Redactor{}
	for _, s := range secrets {
		r.Register(s)
	}
	return r
}
func (r *Redactor) Register(s string) {
	if len(strings.TrimSpace(s)) < 4 {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.secrets = append(r.secrets, s)
	sort.Slice(r.secrets, func(i, j int) bool { return len(r.secrets[i]) > len(r.secrets[j]) })
}

var patterns = []struct {
	re  *regexp.Regexp
	sub string
}{
	{regexp.MustCompile(`://([^:/@\s]+):([^@\s]+)@`), "://$1:[REDACTED]@"},
	{regexp.MustCompile(`(?i)([?&](?:token|api_key|key|secret|password|auth|access_token)=)[^&#\s]+`), "${1}[REDACTED]"},
	{regexp.MustCompile(`\b(?:bot)?[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b`), "[REDACTED_TELEGRAM_TOKEN]"},
	{regexp.MustCompile(`\b(?:sk-(?:proj-)?|gsk_)[A-Za-z0-9_-]{20,}\b`), "[REDACTED_API_KEY]"},
	{regexp.MustCompile(`(?i)((?:authorization|cookie|set-cookie)\s*[:=]\s*)(?:Bearer\s+|Basic\s+)?[^\r\n,;]+`), "${1}[REDACTED]"},
	{regexp.MustCompile(`(?i)((?:password|passwd|secret|api_key|apikey|token|access_token|refresh_token|bot_token|webhook_secret)["']?\s*[:=]\s*["']?)[^\s,"';}&]+`), "${1}[REDACTED]"},
}

func (r *Redactor) Text(s string) string {
	r.mu.RLock()
	for _, secret := range r.secrets {
		s = strings.ReplaceAll(s, secret, "[REDACTED]")
		s = strings.ReplaceAll(s, url.QueryEscape(secret), "[REDACTED]")
	}
	r.mu.RUnlock()
	for _, p := range patterns {
		s = p.re.ReplaceAllString(s, p.sub)
	}
	return s
}

type writer struct {
	out io.Writer
	r   *Redactor
}

func (w writer) Write(b []byte) (int, error) {
	_, e := io.WriteString(w.out, w.r.Text(string(b)))
	return len(b), e
}
func Setup(s config.Settings) *Redactor {
	r := NewRedactor()
	v := reflect.ValueOf(s)
	t := v.Type()
	for i := 0; i < v.NumField(); i++ {
		name := strings.ToLower(t.Field(i).Name)
		if v.Field(i).Kind() == reflect.String && (strings.Contains(name, "key") || strings.Contains(name, "token") || strings.Contains(name, "password") || strings.Contains(name, "secret")) {
			r.Register(v.Field(i).String())
		}
	}
	level := slog.LevelInfo
	if s.BotLogLevel == "ERROR" {
		level = slog.LevelError
	} else if s.BotLogLevel == "TRACE" {
		level = slog.Level(-8)
	}
	output := writer{os.Stdout, r}
	log.SetOutput(output)
	slog.SetDefault(slog.New(slog.NewJSONHandler(output, &slog.HandlerOptions{Level: level})))
	return r
}
func SafeJSON(v any) string {
	b, e := json.Marshal(v)
	if e != nil {
		return "[UNSERIALIZABLE]"
	}
	return string(b)
}
func Failure(ctx context.Context, message string, err error) {
	name := "nil"
	if err != nil {
		name = reflect.TypeOf(err).String()
	}
	slog.ErrorContext(ctx, message, "error_class", name)
}
