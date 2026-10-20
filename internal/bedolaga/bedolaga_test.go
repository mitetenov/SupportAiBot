package bedolaga

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/jackc/pgx/v5/pgxpool"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"supportbot/internal/bot"
	"supportbot/internal/diagnostics"
	"supportbot/internal/domain"
	"supportbot/internal/storage"
	"supportbot/internal/telegram"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func testStore(t *testing.T) (*storage.Store, string) {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		dsn = os.Getenv("SUPPORTBOT_TEST_DATABASE_URL")
	}
	if dsn == "" {
		t.Skip("SUPPORTBOT_TEST_DATABASE_URL enables isolated Docker PostgreSQL tests")
	}
	ctx := context.Background()
	pool, e := pgxpool.New(ctx, dsn)
	if e != nil {
		t.Fatal(e)
	}
	schema := fmt.Sprintf("ticket_parity_%d", time.Now().UnixNano())
	if _, e = pool.Exec(ctx, "CREATE SCHEMA "+schema); e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { _, _ = pool.Exec(ctx, "DROP SCHEMA "+schema+" CASCADE"); pool.Close() })
	parsed, _ := url.Parse(dsn)
	q := parsed.Query()
	q.Set("search_path", schema+",public")
	parsed.RawQuery = q.Encode()
	isolated := parsed.String()
	s, e := storage.New(ctx, isolated)
	if e != nil {
		t.Fatal(e)
	}
	if e = s.Migrate(ctx, 3); e != nil {
		t.Fatal(e)
	}
	t.Cleanup(s.Close)
	return s, isolated
}

type panelFixture struct {
	mu           sync.Mutex
	ticket       Ticket
	posts        int
	gets         int
	lookupCalls  int
	lookupStatus int
	telegramID   any
	blockReply   bool
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

type gatedModel struct {
	domain.LLM
	calls   atomic.Int64
	keysMu  sync.Mutex
	keys    []int64
	started chan struct{}
	release chan struct{}
}

func (m *gatedModel) Chat(ctx context.Context, text string, key int64) (domain.Reply, error) {
	m.calls.Add(1)
	m.keysMu.Lock()
	m.keys = append(m.keys, key)
	m.keysMu.Unlock()
	if m.started != nil {
		select {
		case m.started <- struct{}{}:
		default:
		}
	}
	if m.release != nil {
		select {
		case <-ctx.Done():
			return domain.Reply{}, ctx.Err()
		case <-m.release:
		}
	}
	return domain.Reply{Text: "answer"}, nil
}
func (m *gatedModel) SupportsImages() bool { return false }
func (m *gatedModel) SupportsPDF() bool    { return false }
func answerer(store domain.Store, client TicketAPI, model domain.LLM) *Answerer {
	rate := bot.NewRateLimiter()
	rate.Interval = 0
	return NewAnswerer(context.Background(), client, model, store, rate, nil, nil, nil, bot.NewConversationState(30*time.Minute, 6*time.Hour), 2)
}
func TestRealDBWebhookAndPollIdempotencyAndRestart(t *testing.T) {
	store, dsn := testStore(t)
	panel, client := newPanel(t)
	model := &gatedModel{started: make(chan struct{}, 1), release: make(chan struct{})}
	a := answerer(store, client, model)
	secret := "webhook-test"
	body := []byte(`{"ticket_id":7}`)
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	handler := Webhook(a, secret)
	req := httptest.NewRequest("POST", "/webhook", strings.NewReader(string(body)))
	req.Header.Set("X-Webhook-Event", "ticket.created")
	req.Header.Set("X-Webhook-Signature", "sha256="+hex.EncodeToString(mac.Sum(nil)))
	recorder := httptest.NewRecorder()
	handler(recorder, req)
	if recorder.Code != 200 || !strings.Contains(recorder.Body.String(), "accepted") {
		t.Fatal("webhook failed")
	}
	select {
	case <-model.started:
	case <-time.After(time.Second):
		t.Fatal("model not started")
	}
	for i := 0; i < 20; i++ {
		a.Schedule(7)
	}
	a.Sweep(context.Background(), 50)
	close(model.release)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	a.Drain(ctx)
	panel.mu.Lock()
	posts := panel.posts
	panel.mu.Unlock()
	if posts != 1 || model.calls.Load() != 1 {
		t.Fatalf("duplicates: posts=%d models=%d", posts, model.calls.Load())
	}
	progress, e := store.TicketProgress(ctx, 7)
	if e != nil || progress.LastAnsweredMessageID != 10 || progress.LastBotReplyMessageID != 101 {
		t.Fatalf("progress %+v error %v", progress, e)
	}
	fresh, e := storage.New(ctx, dsn)
	if e != nil {
		t.Fatal(e)
	}
	defer fresh.Close()
	restartModel := &gatedModel{}
	restarted := answerer(fresh, client, restartModel)
	restarted.Handle(ctx, 7)
	if restartModel.calls.Load() != 0 {
		t.Fatal("restart reanswered persisted turn")
	}
	model.keysMu.Lock()
	key := model.keys[0]
	model.keysMu.Unlock()
	if key != 5_000_000_042 {
		t.Fatalf("panel ID used instead of Telegram ID: %d", key)
	}
}
func TestRealDBOperatorOwnershipPersistsAndExpires(t *testing.T) {
	store, dsn := testStore(t)
	panel, client := newPanel(t)
	ctx := context.Background()
	botID, answered := int64(15), int64(10)
	if e := store.RecordTicketReply(ctx, 7, &botID, &answered); e != nil {
		t.Fatal(e)
	}
	at := time.Now().UTC()
	if e := store.RecordHumanReply(ctx, 7, 20, at); e != nil {
		t.Fatal(e)
	}
	panel.mu.Lock()
	panel.ticket.Messages = append(panel.ticket.Messages, TicketMessage{ID: 20, Admin: true, Text: "operator"}, TicketMessage{ID: 21, Text: "user followup"})
	panel.mu.Unlock()
	fresh, e := storage.New(ctx, dsn)
	if e != nil {
		t.Fatal(e)
	}
	defer fresh.Close()
	model := &gatedModel{}
	a := answerer(fresh, client, model)
	a.Handle(ctx, 7)
	if model.calls.Load() != 0 {
		t.Fatal("restart ignored operator takeover")
	}
	a.Now = func() time.Time { return at.Add(31 * time.Minute) }
	a.Handle(ctx, 7)
	if model.calls.Load() != 1 {
		t.Fatal("operator TTL never expired")
	}
	p, _ := fresh.TicketProgress(ctx, 7)
	if p.LastAnsweredMessageID != 21 {
		t.Fatal("followup watermark not advanced")
	}
}
func TestRealDBStaleModelReplyDroppedForHumanOrClose(t *testing.T) {
	for _, reason := range []string{"human", "closed", "new-user"} {
		t.Run(reason, func(t *testing.T) {
			store, _ := testStore(t)
			panel, client := newPanel(t)
			model := &gatedModel{started: make(chan struct{}, 1), release: make(chan struct{})}
			a := answerer(store, client, model)
			done := make(chan struct{})
			go func() { a.Handle(context.Background(), 7); close(done) }()
			select {
			case <-model.started:
			case <-time.After(time.Second):
				t.Fatal("not started")
			}
			panel.mu.Lock()
			switch reason {
			case "human":
				panel.ticket.Messages = append(panel.ticket.Messages, TicketMessage{ID: 11, Admin: true, Text: "human"})
			case "closed":
				panel.ticket.Status = "closed"
			case "new-user":
				panel.ticket.Messages = append(panel.ticket.Messages, TicketMessage{ID: 11, Text: "new info"})
			}
			panel.mu.Unlock()
			close(model.release)
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("stuck")
			}
			panel.mu.Lock()
			posts := panel.posts
			panel.mu.Unlock()
			if posts != 0 {
				t.Fatal("stale answer posted")
			}
			p, _ := store.TicketProgress(context.Background(), 7)
			if p.LastAnsweredMessageID != 0 {
				t.Fatal("stale turn persisted answered")
			}
			if reason == "new-user" {
				a.mu.Lock()
				rerun := a.rerun[7]
				a.mu.Unlock()
				if !rerun {
					t.Fatal("new info lost without rerun")
				}
			}
		})
	}
}
func TestRealDBUnknownLookupNeverCreatesCabinetIdentityAndFailureBackoff(t *testing.T) {
	store, _ := testStore(t)
	panel, client := newPanel(t)
	model := &gatedModel{}
	a := answerer(store, client, model)
	panel.lookupStatus = 502
	a.Handle(context.Background(), 7)
	if model.calls.Load() != 0 {
		t.Fatal("upstream failure minted synthetic identity")
	}
	panel.lookupStatus = 0
	panel.telegramID = nil
	panel.replyStatus = 403
	a.Handle(context.Background(), 7)
	a.Handle(context.Background(), 7)
	if model.calls.Load() != 1 {
		t.Fatal("failed reply did not back off")
	}
	model.keysMu.Lock()
	key := model.keys[0]
	model.keysMu.Unlock()
	if key != -12 {
		t.Fatalf("known cabinet key %d", key)
	}
	a.Now = func() time.Time { return time.Now().Add(2 * time.Minute) }
	panel.replyStatus = 0
	a.Handle(context.Background(), 7)
	if model.calls.Load() != 2 {
		t.Fatal("backoff never retries")
	}
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

type mediaAPI struct {
	failure atomic.Bool
	uploads atomic.Int64
}

func (a *mediaAPI) Call(_ context.Context, method string, payload any, out any) error {
	if method == "createForumTopic" && out != nil {
		json.Unmarshal([]byte(`{"message_thread_id":71}`), out)
	}
	return nil
}
func (a *mediaAPI) Upload(_ context.Context, method string, fields map[string]string, field, name string, reader io.Reader, out any) error {
	a.uploads.Add(1)
	if a.failure.Load() {
		return errors.New("local upload unavailable")
	}
	b, e := io.ReadAll(reader)
	if e != nil || string(b) != "media-bytes" {
		return errors.New("media bytes changed")
	}
	if out != nil {
		json.Unmarshal([]byte(`{"message_id":72}`), out)
	}
	return nil
}
func (a *mediaAPI) Download(context.Context, string, int64) ([]byte, error) { return nil, nil }
func TestRealDBFailedMediaRemainsRetryableAfterAnsweredRestart(t *testing.T) {
	store, dsn := testStore(t)
	panel, client := newPanel(t)
	panel.ticket.Messages[0].HasMedia = true
	panel.ticket.Messages[0].MediaType = "document"
	mediaServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "media-bytes") }))
	defer mediaServer.Close()
	wrapped := &mediaClient{TicketAPI: client, media: &telegram.Media{Type: "document", URL: mediaServer.URL, Filename: "file.bin"}}
	api := &mediaAPI{}
	api.failure.Store(true)
	forward := &bot.Forwarder{Sender: telegram.NewSender(api), Store: store, Group: -100}
	model := &gatedModel{}
	a := answerer(store, wrapped, model)
	a.Forwarder = forward
	a.Handle(context.Background(), 7)
	p, e := store.TicketProgress(context.Background(), 7)
	if e != nil || p.LastAnsweredMessageID != 10 || p.PendingMediaMessageID != 10 || p.LastMirroredMediaMessageID != 0 {
		t.Fatalf("failed media lost retry: %+v error%v", p, e)
	}
	fresh, e := storage.New(context.Background(), dsn)
	if e != nil {
		t.Fatal(e)
	}
	defer fresh.Close()
	api.failure.Store(false)
	restart := answerer(fresh, wrapped, model)
	restart.Forwarder = &bot.Forwarder{Sender: telegram.NewSender(api), Store: fresh, Group: -100}
	restart.Handle(context.Background(), 7)
	p, e = fresh.TicketProgress(context.Background(), 7)
	if e != nil || p.PendingMediaMessageID != 0 || p.LastMirroredMediaMessageID != 10 || model.calls.Load() != 1 {
		t.Fatalf("answered-ticket media not recovered: %+v models%d error%v", p, model.calls.Load(), e)
	}
}

type mediaClient struct {
	TicketAPI
	media *telegram.Media
}

func (c *mediaClient) DescribeMedia(context.Context, int64, int64, string) *telegram.Media {
	return c.media
}
func TestRealDBUserMessageDuringPOSTGetsScheduledRerun(t *testing.T) {
	store, _ := testStore(t)
	panel, client := newPanel(t)
	panel.onReply = func(ticket *Ticket) {
		if len(ticket.Messages) == 1 {
			ticket.Messages = append(ticket.Messages, TicketMessage{ID: 11, Text: "new while posting"})
		}
	}
	model := &gatedModel{}
	a := answerer(store, client, model)
	a.Schedule(7)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	a.Drain(ctx)
	p, e := store.TicketProgress(ctx, 7)
	panel.mu.Lock()
	posts := panel.posts
	panel.mu.Unlock()
	if e != nil || p.LastAnsweredMessageID != 11 || posts != 2 || model.calls.Load() != 2 {
		t.Fatalf("narrow POST race lost: %+v posts%d models%d error%v", p, posts, model.calls.Load(), e)
	}
}
func TestReplyPhotoMultipartThenOneTicketWrite(t *testing.T) {
	uploads, replies := 0, 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-API-Key") != "local-key" {
			t.Error("API auth absent")
		}
		switch r.URL.Path {
		case "/upload":
			uploads++
			if e := r.ParseMultipartForm(1 << 20); e != nil {
				t.Error(e)
			}
			if r.FormValue("media_type") != "photo" {
				t.Error("media type missing")
			}
			file, header, e := r.FormFile("file")
			if e != nil {
				t.Error(e)
				return
			}
			defer file.Close()
			b, _ := io.ReadAll(file)
			if string(b) != "exact-image" || header.Filename != "support-photo.png" || header.Header.Get("Content-Type") != "image/png" {
				t.Error("multipart changed")
			}
			io.WriteString(w, `{"file_id":"panel-photo-id","media_type":"photo"}`)
		case "/tickets/7/reply":
			replies++
			var p map[string]any
			json.NewDecoder(r.Body).Decode(&p)
			if p["media_file_id"] != "panel-photo-id" || p["media_caption"] != "caption" {
				t.Error("upload not attached")
			}
			io.WriteString(w, `{"message":{"id":99}}`)
		default:
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	c := NewClient(server.URL, "local-key", server.Client())
	reply := c.ReplyPhoto(context.Background(), 7, "caption", base64.StdEncoding.EncodeToString([]byte("exact-image")), "image/png")
	if reply == nil || reply.MessageID == nil || *reply.MessageID != 99 || uploads != 1 || replies != 1 {
		t.Fatal("photo write flow failed")
	}
	if c.ReplyPhoto(context.Background(), 7, "caption", "invalid base64", "image/png") != nil || uploads != 1 {
		t.Fatal("invalid base64 reached upstream")
	}
}

type cappedClient struct {
	TicketAPI
	active atomic.Int64
	peak   atomic.Int64
}

func (c *cappedClient) GetTicket(ctx context.Context, id int64) *Ticket {
	n := c.active.Add(1)
	defer c.active.Add(-1)
	for {
		peak := c.peak.Load()
		if peak >= n || c.peak.CompareAndSwap(peak, n) {
			break
		}
	}
	select {
	case <-ctx.Done():
	case <-time.After(5 * time.Millisecond):
	}
	return nil
}
func TestTicketConcurrencyCapCoversUpstreamReads(t *testing.T) {
	c := &cappedClient{}
	a := NewAnswerer(context.Background(), c, nil, nil, nil, nil, nil, nil, nil, 2)
	var wg sync.WaitGroup
	for id := int64(1); id <= 20; id++ {
		wg.Add(1)
		go func(ticket int64) { defer wg.Done(); a.Handle(context.Background(), ticket) }(id)
	}
	wg.Wait()
	if peak := c.peak.Load(); peak != 2 {
		t.Fatalf("upstream peak=%d expected configured2", peak)
	}
}
func TestRealDBShutdownCancelsStuckTicketWithoutWatermark(t *testing.T) {
	store, _ := testStore(t)
	panel, client := newPanel(t)
	model := &gatedModel{started: make(chan struct{}, 1), release: make(chan struct{})}
	a := answerer(store, client, model)
	a.Schedule(7)
	select {
	case <-model.started:
	case <-time.After(time.Second):
		t.Fatal("not started")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	a.Drain(ctx)
	finished := make(chan struct{})
	go func() { a.wg.Wait(); close(finished) }()
	select {
	case <-finished:
	case <-time.After(time.Second):
		t.Fatal("ticket ignored cancellation")
	}
	p, _ := store.TicketProgress(context.Background(), 7)
	panel.mu.Lock()
	posts := panel.posts
	panel.mu.Unlock()
	if p.LastAnsweredMessageID != 0 || posts != 0 {
		t.Fatal("cancelled ticket marked answered")
	}
}
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
