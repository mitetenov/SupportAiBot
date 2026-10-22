package bot

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"strings"
	"supportbot/internal/domain"
	"supportbot/internal/telegram"
	"sync"
	"testing"
	"time"
)

type fakeAPI struct {
	mu       sync.Mutex
	calls    []apiCall
	download []byte
	file     telegram.File
	failure  error
}
type apiCall struct {
	Method  string
	Payload map[string]any
}

func (a *fakeAPI) Call(ctx context.Context, method string, payload any, out any) error {
	b, _ := json.Marshal(payload)
	var p map[string]any
	json.Unmarshal(b, &p)
	a.mu.Lock()
	a.calls = append(a.calls, apiCall{method, p})
	a.mu.Unlock()
	if method == "createForumTopic" && out != nil {
		json.Unmarshal([]byte(`{"message_thread_id":71}`), out)
	}
	if method == "copyMessage" && out != nil {
		json.Unmarshal([]byte(`{"message_id":72}`), out)
	}
	if method == "getFile" && out != nil {
		b, _ = json.Marshal(a.file)
		json.Unmarshal(b, out)
	}
	return a.failure
}
func (a *fakeAPI) Upload(context.Context, string, map[string]string, string, string, io.Reader, any) error {
	return nil
}
func (a *fakeAPI) Download(context.Context, string, int64) ([]byte, error) { return a.download, nil }
func (a *fakeAPI) snapshot() []apiCall {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]apiCall(nil), a.calls...)
}

type memoryStore struct {
	domain.Store
	mu      sync.Mutex
	topics  map[int64]domain.Topic
	maps    []domain.MessageMapping
	profile []domain.User
}

func (s *memoryStore) UpsertUser(_ context.Context, u domain.User) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.profile = append(s.profile, u)
	return nil
}
func (s *memoryStore) TopicByUser(_ context.Context, id int64) (*domain.Topic, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	v, ok := s.topics[id]
	if !ok {
		return nil, nil
	}
	return &v, nil
}
func (s *memoryStore) TopicByID(_ context.Context, id int64) (*domain.Topic, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, v := range s.topics {
		if v.TopicID == id {
			return &v, nil
		}
	}
	return nil, nil
}
func (s *memoryStore) SaveTopic(_ context.Context, v domain.Topic) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.topics[v.UserID] = v
	return nil
}
func (s *memoryStore) DeleteTopic(_ context.Context, user, topic int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if v := s.topics[user]; v.TopicID == topic {
		delete(s.topics, user)
	}
	return nil
}
func (s *memoryStore) SetActiveTicket(_ context.Context, user int64, ticket *int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	v, ok := s.topics[user]
	if ok {
		v.ActiveTicketID = ticket
		s.topics[user] = v
	}
	return nil
}
func (s *memoryStore) SaveMessageMapping(_ context.Context, m domain.MessageMapping) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.maps = append(s.maps, m)
	return nil
}
func (s *memoryStore) MappingByTopicMessage(_ context.Context, topic, message int64) (*domain.MessageMapping, error) {
	for _, v := range s.maps {
		if v.TopicMessageID == message && (topic == 0 || topic == v.TopicID) {
			return &v, nil
		}
	}
	return nil, nil
}
func (s *memoryStore) MappingByUserMessage(_ context.Context, chat, message int64) (*domain.MessageMapping, error) {
	for _, v := range s.maps {
		if v.UserChatID == chat && v.UserMessageID == message {
			return &v, nil
		}
	}
	return nil, nil
}

type modelStub struct {
	domain.LLM
	mu      sync.Mutex
	calls   int
	query   string
	key     int64
	reply   domain.Reply
	started chan struct{}
	release chan struct{}
}

func (m *modelStub) Chat(ctx context.Context, q string, id int64) (domain.Reply, error) {
	m.mu.Lock()
	m.calls++
	m.query = q
	m.key = id
	m.mu.Unlock()
	if m.started != nil {
		select {
		case m.started <- struct{}{}:
		default:
		}
	}
	if m.release != nil {
		select {
		case <-m.release:
		case <-ctx.Done():
			return domain.Reply{}, ctx.Err()
		}
	}
	return m.reply, nil
}
func (m *modelStub) SupportsImages() bool { return false }
func (m *modelStub) SupportsPDF() bool    { return false }

type historyStub struct {
	domain.History
	cleared int64
}

func (h *historyStub) Clear(_ context.Context, id int64) error { h.cleared = id; return nil }
func fixture(t *testing.T) (*Router, *fakeAPI, *memoryStore, *modelStub) {
	t.Helper()
	api := &fakeAPI{}
	store := &memoryStore{topics: map[int64]domain.Topic{42: {UserID: 42, TopicID: 71}}}
	model := &modelStub{reply: domain.Reply{Text: "answer"}}
	sender := telegram.NewSender(api)
	state := NewConversationState(30*time.Minute, 6*time.Hour)
	forward := &Forwarder{Sender: sender, Store: store, Group: -100}
	pipeline := &Pipeline{LLM: model, Sender: sender, State: state, Forwarder: forward, Rate: NewRateLimiter()}
	buffer := NewBuffer(context.Background(), time.Hour, 5, pipeline.Handle)
	t.Cleanup(buffer.Shutdown)
	return &Router{Sender: sender, LLM: model, Store: store, History: &historyStub{}, State: state, Forwarder: forward, Pipeline: pipeline, Buffer: buffer, Group: -100, Admins: map[int64]bool{}}, api, store, model
}
func TestCatchAllRoutingKeepsPythonGroupBehaviorAndUserIdentity(t *testing.T) {
	r, _, store, model := fixture(t)
	user := &telegram.User{ID: 5_000_000_042}
	r.Handle(context.Background(), telegram.Update{Message: &telegram.Message{ID: 1, Chat: telegram.Chat{ID: -200, Type: "supergroup"}, From: user, Text: "personal subscription"}})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	r.Buffer.Drain(ctx)
	if len(store.profile) != 1 || model.key != user.ID || model.query != "personal subscription" {
		t.Fatalf("baseline identity or catch-all route changed: key=%d query=%q", model.key, model.query)
	}
}
func TestStartHelpUnknownAndAdminAuthorization(t *testing.T) {
	r, api, _, _ := fixture(t)
	ctx := context.Background()
	r.State.RecordOperator(42)
	r.State.RecordQuery(42, "old", domain.FAQContext{})
	for _, text := range []string{"/start", "/help", "/stats clear", "/gaps clear", "/unknown"} {
		r.Handle(ctx, telegram.Update{Message: &telegram.Message{ID: 1, Chat: telegram.Chat{ID: 42, Type: "private"}, From: &telegram.User{ID: 42}, Text: text}})
	}
	if r.History.(*historyStub).cleared != 42 || r.State.OperatorActive(42) || r.State.LastQuery(42) != nil {
		t.Fatal("start reset failed")
	}
	messages := []string{}
	for _, c := range api.snapshot() {
		if c.Method == "sendMessage" {
			messages = append(messages, c.Payload["text"].(string))
		}
	}
	if len(messages) != 5 || messages[0] != Message("bot.start.welcome") || messages[1] != Message("bot.help") || messages[2] != Message("bot.unknown.command") {
		t.Fatalf("commands %v", messages)
	}
}
func TestOperatorReplyMappingReactionAndCaptionMedia(t *testing.T) {
	r, api, store, _ := fixture(t)
	store.maps = []domain.MessageMapping{{TopicID: 71, TopicMessageID: 11, UserChatID: 42, UserMessageID: 9}}
	ctx := context.Background()
	operator := &telegram.User{ID: 777}
	r.Handle(ctx, telegram.Update{Message: &telegram.Message{ID: 12, ThreadID: 71, Chat: telegram.Chat{ID: -100}, From: operator, Text: "help", ReplyTo: &telegram.Message{ID: 11}}})
	r.Handle(ctx, telegram.Update{Reaction: &telegram.Reaction{Chat: telegram.Chat{ID: -100}, MessageID: 11, New: []json.RawMessage{json.RawMessage(`{"type":"emoji","emoji":"👍"}`)}}})
	r.Handle(ctx, telegram.Update{Message: &telegram.Message{ID: 13, ThreadID: 71, Chat: telegram.Chat{ID: -100}, From: operator, Caption: "caption", Photo: []telegram.File{{ID: "photo"}}}})
	var reply, reaction, copied bool
	for _, c := range api.snapshot() {
		if c.Method == "sendMessage" && c.Payload["chat_id"] == float64(42) && c.Payload["reply_to_message_id"] == float64(9) {
			reply = true
		}
		if c.Method == "setMessageReaction" && c.Payload["message_id"] == float64(9) {
			reaction = true
		}
		if c.Method == "copyMessage" && c.Payload["message_id"] == float64(13) {
			copied = true
		}
	}
	if !reply || !reaction || !copied || !r.State.OperatorActive(42) {
		t.Fatal("operator mapping/media/reaction failed")
	}
}
func TestCabinetTopicCannotFalselyConfirmUserDelivery(t *testing.T) {
	r, api, store, _ := fixture(t)
	store.topics[-44] = domain.Topic{UserID: -44, TopicID: 90}
	r.Handle(context.Background(), telegram.Update{Message: &telegram.Message{ID: 1, ThreadID: 90, Chat: telegram.Chat{ID: -100}, From: &telegram.User{ID: 7}, Text: "reply"}})
	for _, c := range api.snapshot() {
		if c.Method == "sendMessage" && c.Payload["chat_id"] == float64(-44) {
			t.Fatal("sent to synthetic identity")
		}
		if c.Method == "sendMessage" && c.Payload["text"] == Message("support.sent") {
			t.Fatal("false delivery confirmation")
		}
	}
}
func TestAskDoesNotSetUsersLastQueryAndKeepsOperatorOwnership(t *testing.T) {
	r, api, _, model := fixture(t)
	r.Handle(context.Background(), telegram.Update{Message: &telegram.Message{ID: 1, ThreadID: 71, Chat: telegram.Chat{ID: -100}, From: &telegram.User{ID: 7}, Text: "/ask@support_bot\ncheck now"}})
	if model.query != "check now" || model.key != 42 || !r.State.OperatorActive(42) || r.State.LastQuery(42) != nil {
		t.Fatal("ask state mismatch")
	}
	found := false
	for _, c := range api.snapshot() {
		if c.Method == "sendMessage" && c.Payload["text"] == "answer" {
			found = true
		}
	}
	if !found {
		t.Fatal("ask not delivered")
	}
}
func TestBufferSplitsEverySecondAttachmentAndTracksAuthoredText(t *testing.T) {
	var mu sync.Mutex
	var got []Batch
	b := NewBuffer(context.Background(), time.Hour, 5, func(_ context.Context, v Batch) { mu.Lock(); defer mu.Unlock(); got = append(got, v) })
	m := telegram.Message{ID: 1, From: &telegram.User{ID: 42}, Chat: telegram.Chat{ID: 42}}
	b.Submit(42, BufferedMessage{Message: m, Text: "generated screenshot", Image: "image"})
	m.ID = 2
	b.Submit(42, BufferedMessage{Message: m, Text: "caption", UserText: "caption", PDF: "pdf"})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	b.Drain(ctx)
	if len(got) != 2 {
		t.Fatalf("attachment loss %d", len(got))
	}
	for _, v := range got {
		if v.Image != "" && v.UserText != "" {
			t.Fatal("generated text reported authored")
		}
		if v.PDF != "" && v.UserText != "caption" {
			t.Fatal("caption lost")
		}
	}
}
func TestStateExpiryAndHumanWordBoundaries(t *testing.T) {
	now := time.Now()
	s := NewConversationState(time.Minute, time.Hour)
	s.Now = func() time.Time { return now }
	s.RecordOperator(42)
	s.RecordQuery(42, "test", domain.FAQContext{})
	now = now.Add(time.Minute)
	if s.OperatorActive(42) {
		t.Fatal("suppression boundary")
	}
	now = now.Add(time.Hour)
	if s.LastQuery(42) != nil {
		t.Fatal("query TTL")
	}
	if RequestsHuman("живу в Германии, болит живот") || !RequestsHuman("нужен живой оператор") {
		t.Fatal("human morphology")
	}
	if _, ok := ParseAsk("/asking hello"); ok {
		t.Fatal("ask longer word")
	}
	if strings.Contains(Message("bot.help"), "{0}") {
		t.Fatal("message extraction")
	}
}

func TestOperatorTakeoverDuringModelGenerationDropsStaleTelegramAnswer(t *testing.T) {
	r, api, _, model := fixture(t)
	model.started = make(chan struct{}, 1)
	model.release = make(chan struct{})
	done := make(chan struct{})
	go func() {
		r.Pipeline.Handle(context.Background(), Batch{Last: telegram.Message{ID: 5, Chat: telegram.Chat{ID: 42}}, User: &telegram.User{ID: 42}, Text: "question", UserText: "question", IDs: []int64{5}})
		close(done)
	}()
	select {
	case <-model.started:
	case <-time.After(time.Second):
		t.Fatal("model not started")
	}
	r.State.RecordOperator(42)
	close(model.release)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("pipeline stuck")
	}
	for _, c := range api.snapshot() {
		if c.Method == "sendMessage" && c.Payload["chat_id"] == float64(42) && c.Payload["text"] == "answer" {
			t.Fatal("stale AI response interrupted operator")
		}
	}
}
func TestPhotoPDFValidationAndUnsupportedDocuments(t *testing.T) {
	r, api, _, _ := fixture(t)
	ctx := context.Background()
	api.file = telegram.File{Path: "document.pdf"}
	api.download = []byte("%PDF-1.7\nexact bytes")
	data, key := r.downloadPDF(ctx, telegram.File{ID: "file", MIME: "application/pdf"})
	decoded, _ := base64.StdEncoding.DecodeString(data)
	if key != "" || string(decoded) != "%PDF-1.7\nexact bytes" {
		t.Fatal("PDF bytes changed")
	}
	api.download = []byte("not PDF")
	if _, key = r.downloadPDF(ctx, telegram.File{ID: "file", Name: "fake.pdf"}); key != "bot.pdf.invalid" {
		t.Fatal("disguised PDF accepted")
	}
	before := len(api.snapshot())
	if _, key = r.downloadPDF(ctx, telegram.File{ID: "file", Name: "too-big.pdf", Size: 20_000_001}); key != "bot.pdf.too_large" || len(api.snapshot()) != before {
		t.Fatal("declared oversize reached network")
	}
	api.file.Path = "photos/image.png"
	api.download = []byte{0, 128, 255}
	b64, mime, key := r.downloadPhoto(ctx, []telegram.File{{ID: "low"}, {ID: "largest"}})
	decoded, _ = base64.StdEncoding.DecodeString(b64)
	if key != "" || mime != "image/png" || len(decoded) != 3 {
		t.Fatal("photo MIME or bytes")
	}
	calls := api.snapshot()
	last := calls[len(calls)-1]
	if last.Payload["file_id"] != "largest" {
		t.Fatal("not largest photo")
	}
	for _, message := range []telegram.Message{{ID: 5, Photo: []telegram.File{{ID: "photo"}}}, {ID: 6, Document: &telegram.File{ID: "pdf", Name: "file.pdf"}}, {ID: 7, Document: &telegram.File{ID: "doc", Name: "file.zip"}}} {
		message.From = &telegram.User{ID: 42}
		message.Chat.ID = 42
		r.Handle(ctx, telegram.Update{Message: &message})
	}
	if r.Buffer.pending[42] != nil {
		t.Fatal("unsupported media reached model buffer")
	}
}
func TestStartDiscardsPendingUserBurst(t *testing.T) {
	r, _, _, model := fixture(t)
	r.Handle(context.Background(), telegram.Update{Message: &telegram.Message{ID: 1, Chat: telegram.Chat{ID: 42}, From: &telegram.User{ID: 42}, Text: "old question"}})
	r.Handle(context.Background(), telegram.Update{Message: &telegram.Message{ID: 2, Chat: telegram.Chat{ID: 42}, From: &telegram.User{ID: 42}, Text: "/start"}})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	r.Buffer.Drain(ctx)
	if model.calls != 0 {
		t.Fatal("reset answered old buffered question")
	}
}
