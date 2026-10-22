package bedolaga

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"supportbot/internal/diagnostics"
	"sync"
	"testing"
)

type panelFixture struct {
	mu           sync.Mutex
	ticket       Ticket
	posts        int
	gets         int
	lookupCalls  int
	lookupStatus int
	telegramID   any
	onReply      func(*Ticket)
	replyStatus  int
}

func (f *panelFixture) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	switch {
	case r.URL.Path == "/users/12":
		f.lookupCalls++
		if f.lookupStatus != 0 {
			w.WriteHeader(f.lookupStatus)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"telegram_id": f.telegramID, "first_name": "Customer"})
	case strings.HasSuffix(r.URL.Path, "/reply"):
		f.posts++
		if f.replyStatus != 0 {
			w.WriteHeader(f.replyStatus)
			return
		}
		if f.onReply != nil {
			f.onReply(&f.ticket)
		}
		last := int64(100 + f.posts)
		f.ticket.Messages = append(f.ticket.Messages, TicketMessage{ID: last, Admin: true, Text: "answer"})
		f.ticket.Status = "answered"
		_ = json.NewEncoder(w).Encode(map[string]any{"message": map[string]any{"id": last}})
	case r.URL.Path == "/tickets/7":
		f.gets++
		_ = json.NewEncoder(w).Encode(f.ticket)
	case r.URL.Path == "/tickets":
		_ = json.NewEncoder(w).Encode([]map[string]any{{"id": 7}})
	default:
		io.WriteString(w, `{}`)
	}
}
func newPanel(t *testing.T) (*panelFixture, *Client) {
	f := &panelFixture{ticket: Ticket{ID: 7, UserID: 12, Title: "issue", Status: "open", Messages: []TicketMessage{{ID: 10, Text: "question"}}}, telegramID: 5_000_000_042}
	server := httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(server.Close)
	return f, NewClient(server.URL, "test-only-key", server.Client())
}

func TestReplyIsSentOnceAfterGatewayFailureAndAcceptedNoID(t *testing.T) {
	for _, response := range []struct {
		status   int
		body     string
		accepted bool
	}{{502, `{}`, false}, {201, `{"accepted":true}`, true}, {200, `invalid json`, true}} {
		calls := 0
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls++
			w.WriteHeader(response.status)
			io.WriteString(w, response.body)
		}))
		client := NewClient(server.URL, "test", server.Client())
		posted := client.Reply(context.Background(), 7, "answer")
		server.Close()
		if calls != 1 || (posted != nil) != response.accepted {
			t.Fatalf("duplicate or lost accepted write: %d %+v", calls, posted)
		}
		if posted != nil && posted.MessageID != nil {
			t.Fatal("fabricated reply ID")
		}
	}
}
func TestLookupNullVsErrorAndOriginValidation(t *testing.T) {
	panel, client := newPanel(t)
	panel.lookupStatus = 502
	if client.Resolve(context.Background(), 12).Known {
		t.Fatal("502 treated known")
	}
	panel.lookupStatus = 0
	panel.telegramID = nil
	if v := client.Resolve(context.Background(), 12); !v.Known || v.TelegramID != nil {
		t.Fatal("null account lost")
	}
	panel.telegramID = 123
	if v := client.Resolve(context.Background(), 12); v.TelegramID == nil || *v.TelegramID != 123 {
		t.Fatal("failed/null lookup wrongly cached")
	}
	client.BaseURL = "https://panel.test/prefix"
	if client.ResolveMediaURL("media/file") != "https://panel.test/prefix/media/file" {
		t.Fatal("base path lost")
	}
	for _, raw := range []string{"http://panel.test/file", "https://other.test/file", "https://panel.test:444/file"} {
		if client.ResolveMediaURL(raw) != "" {
			t.Fatalf("unsafe media origin %s", raw)
		}
	}
}
func TestWebhookMalformedAndSignatureRejection(t *testing.T) {
	s := &scheduleStub{}
	handler := Webhook(s, "secret")
	for _, item := range []struct {
		body, event, signature string
		status                 int
	}{{`{"ticket_id":7}`, "ticket.created", "bad", 403}, {`[]`, "ticket.created", "", 403}} {
		r := httptest.NewRequest("POST", "/", strings.NewReader(item.body))
		r.Header.Set("X-Webhook-Event", item.event)
		r.Header.Set("X-Webhook-Signature", item.signature)
		w := httptest.NewRecorder()
		handler(w, r)
		if w.Code != item.status {
			t.Fatal(w.Code)
		}
	}
	if len(s.ids) != 0 {
		t.Fatal("invalid webhook scheduled")
	}
	handler = Webhook(s, "")
	for _, body := range []string{`[]`, `null`, `{"ticket_id":"no"}`, `broken`} {
		w := httptest.NewRecorder()
		r := httptest.NewRequest("POST", "/", strings.NewReader(body))
		r.Header.Set("X-Webhook-Event", "ticket.created")
		handler(w, r)
		if w.Code != 400 {
			t.Fatalf("body %s status %d", body, w.Code)
		}
	}
}

type scheduleStub struct{ ids []int64 }

func (s *scheduleStub) Schedule(id int64) { s.ids = append(s.ids, id) }

func TestPythonTicketQuestionAndNullNormalization(t *testing.T) {
	raw, e := os.ReadFile("../bot/testdata/python_runtime.json")
	if e != nil {
		t.Fatal(e)
	}
	var fixture struct {
		Tickets []struct {
			Input      Ticket
			Question   string
			Awaits     bool
			LastUserID *int64 `json:"last_user_id"`
		}
	}
	if e = json.Unmarshal(raw, &fixture); e != nil {
		t.Fatal(e)
	}
	for _, c := range fixture.Tickets {
		if c.Input.Question(c.Input.Last()) != c.Question || c.Input.Awaits() != c.Awaits {
			t.Errorf("question/status mismatch input%+v expected%q", c.Input, c.Question)
		}
		user := c.Input.LastUser()
		if (user == nil) != (c.LastUserID == nil) || (user != nil && user.ID != *c.LastUserID) {
			t.Error("latest user mismatch")
		}
	}
}
func TestBedolagaDiagnosticsRespectLevelAndSecrets(t *testing.T) {
	for _, level := range []slog.Level{slog.LevelInfo, diagnostics.TraceLevel} {
		t.Run(level.String(), func(t *testing.T) {
			var logs bytes.Buffer
			previous := slog.Default()
			slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, &slog.HandlerOptions{Level: level})))
			defer slog.SetDefault(previous)
			diagnostics.Register("bedolaga-secret-sentinel")
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(503)
				io.WriteString(w, `{"error":"private-panel-response","api_key":"bedolaga-secret-sentinel"}`)
			}))
			defer server.Close()
			client := NewClient(server.URL, "bedolaga-secret-sentinel", server.Client())
			ctx := diagnostics.WithCorrelation(context.Background(), "ticket-correlation")
			client.Reply(ctx, 7, "private-ticket-question")
			out := logs.String()
			if !strings.Contains(out, "Bedolaga HTTP request failed") || !strings.Contains(out, "ticket-correlation") {
				t.Fatal("failure/correlation absent")
			}
			if strings.Contains(out, "bedolaga-secret-sentinel") {
				t.Fatal("secret leak")
			}
			if level == slog.LevelInfo && (strings.Contains(out, "private-ticket-question") || strings.Contains(out, "private-panel-response")) {
				t.Fatal("INFO private leak")
			}
			if level == diagnostics.TraceLevel && (!strings.Contains(out, "private-ticket-question") || !strings.Contains(out, "request_id")) {
				t.Fatal("TRACE body/request absent")
			}
		})
	}
}
