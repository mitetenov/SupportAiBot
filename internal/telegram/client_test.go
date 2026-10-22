package telegram

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"supportbot/internal/diagnostics"
	"testing"
)

func TestMultipartPreservesBytesAndFields(t *testing.T) {
	content := []byte{0, 1, 2, 255, 128, 10}
	var called bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		if r.URL.Path != "/botfake/sendDocument" {
			t.Errorf("route %s", r.URL.Path)
		}
		if e := r.ParseMultipartForm(1 << 20); e != nil {
			t.Error(e)
			return
		}
		if r.FormValue("chat_id") != "-100" || r.FormValue("message_thread_id") != "42" {
			t.Error("missing topic identity")
		}
		file, h, e := r.FormFile("document")
		if e != nil {
			t.Error(e)
			return
		}
		defer file.Close()
		b, _ := io.ReadAll(file)
		if !bytes.Equal(b, content) || h.Filename != "screenshot.bin" {
			t.Error("bytes or filename changed")
		}
		io.WriteString(w, `{"ok":true,"result":{"message_id":9}}`)
	}))
	defer server.Close()
	c := New("fake", server.Client())
	c.BaseURL = server.URL
	var result Message
	e := c.Upload(context.Background(), "sendDocument", Fields(-100, 42), "document", "screenshot.bin", bytes.NewReader(content), &result)
	if e != nil || result.ID != 9 || !called {
		t.Fatalf("upload %v %+v", e, result)
	}
}
func TestSenderOnlyRetriesEntityErrors(t *testing.T) {
	for _, status := range []struct {
		description string
		calls       int
	}{{"Bad Request: can't parse entities", 2}, {"Forbidden: bot blocked by user", 1}} {
		t.Run(status.description, func(t *testing.T) {
			calls := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				var p map[string]any
				_ = json.NewDecoder(r.Body).Decode(&p)
				if calls == 1 {
					_ = json.NewEncoder(w).Encode(map[string]any{"ok": false, "error_code": 400, "description": status.description})
					return
				}
				if p["text"] != "bold & safe" || p["parse_mode"] != nil {
					t.Errorf("plain fallback %v", p)
				}
				io.WriteString(w, `{"ok":true,"result":{"message_id":1}}`)
			}))
			defer server.Close()
			client := New("fake", server.Client())
			client.BaseURL = server.URL
			NewSender(client).Send(context.Background(), 42, 0, 0, "**bold** & safe")
			if calls != status.calls {
				t.Fatalf("calls %d expected %d", calls, status.calls)
			}
		})
	}
}
func TestMediaRedirectDoesNotLeakKeyToOtherOrigin(t *testing.T) {
	var leaked string
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		leaked = r.Header.Get("X-API-Key")
		io.WriteString(w, "file")
	}))
	defer target.Close()
	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-API-Key") != "sentinel" {
			t.Error("missing original key")
		}
		http.Redirect(w, r, target.URL, http.StatusFound)
	}))
	defer source.Close()
	body, e := OpenMedia(context.Background(), source.Client(), source.URL, map[string]string{"X-API-Key": "sentinel"})
	if e != nil {
		t.Fatal(e)
	}
	defer body.Close()
	b, _ := io.ReadAll(body)
	if leaked != "" || string(b) != "file" {
		t.Fatalf("redirect leaked %q body %q", leaked, b)
	}
}
func TestDownloadBoundAndNoTokenInError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, strings.Repeat("x", 101)) }))
	defer server.Close()
	client := New("secret-sentinel", server.Client())
	client.BaseURL = server.URL
	if _, e := client.Download(context.Background(), "x", 100); e != ErrTooLarge {
		t.Fatalf("expected bounded download: %v", e)
	}
	server.Close()
	if _, e := client.Download(context.Background(), "x", 100); e == nil || strings.Contains(e.Error(), "secret-sentinel") {
		t.Fatal("credential leak or missing transport error")
	}
}
func TestUnknownSizeMediaReaderRejectsRatherThanTruncates(t *testing.T) {
	reader := &boundedMediaReader{reader: strings.NewReader("123456"), remaining: 5}
	data, e := io.ReadAll(reader)
	if e != ErrTooLarge || string(data) != "12345" {
		t.Fatalf("stream boundary bytes%q error%v", data, e)
	}
	reader = &boundedMediaReader{reader: strings.NewReader("12345"), remaining: 5}
	data, e = io.ReadAll(reader)
	if e != nil || string(data) != "12345" {
		t.Fatal("exact size rejected")
	}
}
func TestTelegramDiagnosticsRespectLevelCorrelationAndSecrets(t *testing.T) {
	for _, level := range []slog.Level{slog.LevelInfo, diagnostics.TraceLevel} {
		t.Run(level.String(), func(t *testing.T) {
			var logs bytes.Buffer
			previous := slog.Default()
			slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, &slog.HandlerOptions{Level: level})))
			defer slog.SetDefault(previous)
			diagnostics.Register("secret-sentinel-key")
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				io.WriteString(w, `{"ok":false,"error_code":403,"description":"private-error secret-sentinel-key"}`)
			}))
			defer server.Close()
			client := New("secret-sentinel-key", server.Client())
			client.BaseURL = server.URL
			ctx := diagnostics.WithCorrelation(context.Background(), "test-correlation")
			_ = client.Call(ctx, "sendMessage", map[string]any{"chat_id": 42, "text": "private-customer-text"}, nil)
			out := logs.String()
			if !strings.Contains(out, "Telegram API request failed") || !strings.Contains(out, "test-correlation") {
				t.Fatal("error/correlation absent")
			}
			if strings.Contains(out, "secret-sentinel-key") {
				t.Fatal("credential exposed")
			}
			if level == slog.LevelInfo && (strings.Contains(out, "private-customer-text") || strings.Contains(out, "private-error")) {
				t.Fatal("INFO exposed private data")
			}
			if level == diagnostics.TraceLevel && (!strings.Contains(out, "private-customer-text") || !strings.Contains(out, "request_id")) {
				t.Fatal("TRACE request body or paired request missing")
			}
		})
	}
}
