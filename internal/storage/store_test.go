package storage

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"reflect"
	"strings"
	"supportbot/internal/domain"
	"testing"
	"time"
)

func integrationStore(t *testing.T, pythonSchema bool) *Store {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL required for real PostgreSQL parity")
	}
	ctx := context.Background()
	admin, e := New(ctx, dsn)
	if e != nil {
		t.Fatal(e)
	}
	schema := fmt.Sprintf("go_storage_%d", time.Now().UnixNano())
	if _, e = admin.Pool.Exec(ctx, "CREATE SCHEMA "+schema); e != nil {
		t.Fatal(e)
	}
	u, _ := url.Parse(dsn)
	q := u.Query()
	q.Set("search_path", schema+",public")
	u.RawQuery = q.Encode()
	s, e := New(ctx, u.String())
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { s.Close(); admin.Pool.Exec(ctx, "DROP SCHEMA "+schema+" CASCADE"); admin.Close() })
	if pythonSchema {
		raw, e := os.ReadFile("testdata/python_schema.sql")
		if e != nil {
			t.Fatal(e)
		}
		if _, e = s.Pool.Exec(ctx, string(raw)); e != nil {
			t.Fatal(e)
		}
	}
	return s
}
func TestFilledPythonSchemaMigrateRestart(t *testing.T) {
	s := integrationStore(t, true)
	ctx := context.Background()
	_, e := s.Pool.Exec(ctx, `ALTER TABLE faq ALTER COLUMN embedding TYPE vector(3); INSERT INTO user_names(telegram_id,username,updated_at) VALUES(5000000001,'alice',now()); INSERT INTO topic_mappings(user_id,topic_id,user_name,active_ticket_id,created_at) VALUES(5000000001,17,'Alice',NULL,now()); INSERT INTO chat_messages(telegram_id,role,content,created_at) VALUES(5000000001,'user','Подписка', '2026-01-01T00:00:00Z'),(5000000001,'assistant','Проверьте', '2026-01-01T00:00:00Z'); INSERT INTO bedolaga_ticket_state(ticket_id,last_answered_message_id,last_bot_reply_message_id,last_human_reply_message_id,last_mirrored_media_message_id,pending_media_message_id,last_human_reply_at,updated_at) VALUES(9,41,42,0,40,43,NULL,now()); INSERT INTO faq(id,question,answer,embedding) VALUES('old','Q','A','[1,0,0]')`)
	if e != nil {
		t.Fatal(e)
	}
	if e = s.Migrate(ctx, 3); e != nil {
		t.Fatal(e)
	}
	if e = s.Migrate(ctx, 3); e != nil {
		t.Fatal(e)
	}
	var vector string
	if e = s.Pool.QueryRow(ctx, "SELECT embedding::text FROM faq WHERE id='old'").Scan(&vector); e != nil || vector != "[1,0,0]" {
		t.Fatalf("lost compatible vector: %s %v", vector, e)
	}
	history := NewHistory(s, 20, 7)
	a, e := history.GetHistory(ctx, 5000000001)
	if e != nil {
		t.Fatal(e)
	}
	b, e := NewHistory(s, 20, 7).GetHistory(ctx, 5000000001)
	if e != nil || !reflect.DeepEqual(a, b) || len(a) != 2 || a[0].Role != "user" {
		t.Fatalf("restart history drift: %#v %#v %v", a, b, e)
	}
	p, e := s.TicketProgress(ctx, 9)
	if e != nil || p.LastAnsweredMessageID != 41 || p.LastBotReplyMessageID != 42 || p.LastHumanReplyAt != nil || p.PendingMediaMessageID != 43 {
		t.Fatalf("lost state %#v %v", p, e)
	}
	topic, e := s.TopicByUser(ctx, 5000000001)
	if e != nil || topic.ActiveTicketID != nil {
		t.Fatalf("NULL active ticket changed %#v %v", topic, e)
	}
	if e = s.RecordTicketReply(ctx, 9, nil, nil); e != nil {
		t.Fatal(e)
	}
	p, _ = s.TicketProgress(ctx, 9)
	if p.LastAnsweredMessageID != 41 || p.LastBotReplyMessageID != 42 {
		t.Fatalf("nil erased watermark %#v", p)
	}
	if e = s.RecordMirroredMedia(ctx, 9, 43); e != nil {
		t.Fatal(e)
	}
	p, _ = s.TicketProgress(ctx, 9)
	if p.PendingMediaMessageID != 0 || p.LastMirroredMediaMessageID != 43 {
		t.Fatalf("media retry wrong %#v", p)
	}
	if e = s.Migrate(ctx, 4); e != nil {
		t.Fatal(e)
	}
	var null bool
	if e = s.Pool.QueryRow(ctx, "SELECT embedding IS NULL FROM faq WHERE id='old'").Scan(&null); e != nil || !null {
		t.Fatal("changed dimension must invalidate old embedding", e)
	}
}
func TestLegacyUTCAndMissingColumns(t *testing.T) {
	s := integrationStore(t, true)
	ctx := context.Background()
	_, e := s.Pool.Exec(ctx, `ALTER TABLE message_mappings DROP COLUMN created_at; ALTER TABLE topic_mappings DROP COLUMN active_ticket_id; ALTER TABLE bedolaga_ticket_state DROP COLUMN pending_media_message_id; ALTER TABLE knowledge_gaps ALTER COLUMN first_seen TYPE timestamp without time zone; INSERT INTO knowledge_gaps(user_query,gap_count,first_seen,last_seen) VALUES('q',1,'2020-01-02 03:04:05',now())`)
	if e != nil {
		t.Fatal(e)
	}
	if e = s.Migrate(ctx, 3); e != nil {
		t.Fatal(e)
	}
	var at time.Time
	if e = s.Pool.QueryRow(ctx, "SELECT first_seen FROM knowledge_gaps").Scan(&at); e != nil || at.UTC().Format(time.RFC3339) != "2020-01-02T03:04:05Z" {
		t.Fatal("UTC reinterpretation drift", at, e)
	}
	if e = s.SaveMessageMapping(ctx, domain.MessageMapping{TopicID: 3, TopicMessageID: 4, UserChatID: 5, UserMessageID: 6}); e != nil {
		t.Fatal(e)
	}
	m, e := s.MappingByTopicMessage(ctx, 0, 4)
	if e != nil || m == nil || m.UserChatID != 5 {
		t.Fatal("reaction mapping lookup failed", m, e)
	}
}
func TestHistoryDefensiveCopyAndIsolation(t *testing.T) {
	h := NewHistory(nil, 2, 7)
	ctx := context.Background()
	for _, s := range []string{"a", "b", "c"} {
		if e := h.AddUserMessage(ctx, 1, s); e != nil {
			t.Fatal(e)
		}
	}
	got, _ := h.GetHistory(ctx, 1)
	got[0].Content = "mutate"
	got, _ = h.GetHistory(ctx, 1)
	if len(got) != 2 || got[0].Content != "b" {
		t.Fatal(got)
	}
	h.RecordFAQContext(1, domain.FAQContext{Results: []domain.FAQResult{{Question: "shown"}, {Question: "unused"}}}, []string{"shown"})
	h.RejectLastFAQ(1)
	if !h.GetRejectedFAQQuestions(1)["shown"] || h.GetRejectedFAQQuestions(1)["unused"] {
		t.Fatal("unattributed FAQ rejected")
	}
	h.ClearRejectedFAQsIfNewTopic(1, "Всё равно не работает")
	if !h.GetRejectedFAQQuestions(1)["shown"] {
		t.Fatal("rejection lost state")
	}
	h.ClearRejectedFAQsIfNewTopic(1, "Как подключить другое устройство?")
	if len(h.GetRejectedFAQQuestions(1)) != 0 {
		t.Fatal("new topic retained rejected context")
	}
	h.AddUserMessage(ctx, 2, "other")
	h.Clear(ctx, 1)
	v, _ := h.GetHistory(ctx, 2)
	if len(v) != 1 {
		t.Fatal("cross user clear")
	}
}
func TestRejectionBoundaries(t *testing.T) {
	for _, x := range []string{"Не только", "другое устройство", "не товарищ", "подключить другое"} {
		if IsRejection(x) {
			t.Fatal("false rejection", x)
		}
	}
	for _, x := range []string{"другое!", "это не то.", "Всё ещё не заработало", "по-прежнему"} {
		if !IsRejection(x) {
			t.Fatal("missed rejection", x)
		}
	}
	_ = strings.TrimSpace
}
func TestPythonDifferentialRejection(t *testing.T) {
	raw, e := os.ReadFile("testdata/python_rejection.json")
	if e != nil {
		t.Fatal(e)
	}
	var rows []struct {
		Text     string
		Rejected bool
	}
	if e = json.Unmarshal(raw, &rows); e != nil {
		t.Fatal(e)
	}
	for _, r := range rows {
		if got := IsRejection(r.Text); got != r.Rejected {
			t.Errorf("%q: %v != %v", r.Text, got, r.Rejected)
		}
	}
}
func TestHistoryDatabaseOutageKeepsConversation(t *testing.T) {
	s := integrationStore(t, false)
	s.Close()
	h := NewHistory(s, 20, 7)
	ctx := context.Background()
	if e := h.AddUserMessage(ctx, 1, "question"); e != nil {
		t.Fatal("load/persistence outage blocked turn", e)
	}
	if e := h.AddAssistantMessage(ctx, 1, "answer"); e != nil {
		t.Fatal(e)
	}
	messages, e := h.GetHistory(ctx, 1)
	if e != nil || len(messages) != 2 {
		t.Fatal("memory fallback lost conversation", messages, e)
	}
	if e = h.Clear(ctx, 1); e != nil {
		t.Fatal(e)
	}
	messages, _ = h.GetHistory(ctx, 1)
	if len(messages) != 0 {
		t.Fatal("clear outage retained memory", messages)
	}
}
func TestProviderVectorWidthsPreserved(t *testing.T) {
	for _, width := range []int{1536, 2000} {
		t.Run(fmt.Sprint(width), func(t *testing.T) {
			s := integrationStore(t, true)
			ctx := context.Background()
			if e := s.Migrate(ctx, width); e != nil {
				t.Fatal(e)
			}
			vector := "[1" + strings.Repeat(",0", width-1) + "]"
			if _, e := s.Pool.Exec(ctx, "INSERT INTO faq(id,question,answer,embedding) VALUES('row','Q','A',$1::vector)", vector); e != nil {
				t.Fatal(e)
			}
			if e := s.Migrate(ctx, width); e != nil {
				t.Fatal(e)
			}
			var dimensions int
			if e := s.Pool.QueryRow(ctx, "SELECT vector_dims(embedding) FROM faq WHERE id='row'").Scan(&dimensions); e != nil || dimensions != width {
				t.Fatal("same-width migration lost provider embedding", dimensions, e)
			}
		})
	}
}
