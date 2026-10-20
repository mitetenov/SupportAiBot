// Package storage keeps the existing PostgreSQL schema readable by both runtimes.
package storage

import (
	"context"
	"errors"
	"fmt"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"strings"
	"supportbot/internal/domain"
	"time"
)

type Store struct{ Pool *pgxpool.Pool }

func New(ctx context.Context, dsn string) (*Store, error) {
	p, e := pgxpool.New(ctx, strings.Replace(dsn, "postgresql+asyncpg:", "postgresql:", 1))
	if e != nil {
		return nil, e
	}
	if e = p.Ping(ctx); e != nil {
		p.Close()
		return nil, e
	}
	return &Store{p}, nil
}
func (s *Store) Close() { s.Pool.Close() }

const tables = `
CREATE TABLE IF NOT EXISTS user_names(telegram_id BIGINT PRIMARY KEY,username VARCHAR(255),first_name VARCHAR(255),last_name VARCHAR(255),updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS topic_mappings(user_id BIGINT PRIMARY KEY,topic_id INTEGER NOT NULL,user_name VARCHAR(255),active_ticket_id BIGINT,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS message_mappings(id BIGSERIAL PRIMARY KEY,topic_message_id INTEGER NOT NULL,topic_id INTEGER NOT NULL,user_chat_id BIGINT NOT NULL,user_message_id INTEGER NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_message_mappings_topic_message_id ON message_mappings(topic_message_id);
CREATE INDEX IF NOT EXISTS idx_message_mappings_user_message ON message_mappings(user_chat_id,user_message_id);
CREATE TABLE IF NOT EXISTS chat_messages(id BIGSERIAL PRIMARY KEY,telegram_id BIGINT NOT NULL,role VARCHAR(10) NOT NULL,content TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_chat_messages_telegram_id ON chat_messages(telegram_id,created_at);
CREATE INDEX IF NOT EXISTS ix_chat_messages_created_at ON chat_messages(created_at);
CREATE TABLE IF NOT EXISTS llm_token_usage(id BIGSERIAL PRIMARY KEY,telegram_id BIGINT,prompt_tokens BIGINT,completion_tokens BIGINT,total_tokens BIGINT,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_llm_token_usage_telegram_id ON llm_token_usage(telegram_id);
CREATE TABLE IF NOT EXISTS knowledge_gaps(id BIGSERIAL PRIMARY KEY,user_query VARCHAR(2000) NOT NULL,embedding vector,best_faq_question VARCHAR(2000),max_similarity DOUBLE PRECISION,faq_count INTEGER DEFAULT 0,trigger_reason VARCHAR(20),bot_response VARCHAR(500),gap_count INTEGER NOT NULL DEFAULT 1,first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),telegram_id BIGINT);
CREATE TABLE IF NOT EXISTS faq(id VARCHAR(36) PRIMARY KEY,question VARCHAR(2000) NOT NULL,answer TEXT NOT NULL,embedding vector,keywords VARCHAR(2000),image VARCHAR(255));
CREATE TABLE IF NOT EXISTS faq_metadata(key VARCHAR(50) PRIMARY KEY,val VARCHAR(256) NOT NULL);
CREATE TABLE IF NOT EXISTS bedolaga_ticket_state(ticket_id BIGINT PRIMARY KEY,last_answered_message_id BIGINT NOT NULL,last_bot_reply_message_id BIGINT NOT NULL DEFAULT 0,last_human_reply_message_id BIGINT NOT NULL DEFAULT 0,last_mirrored_media_message_id BIGINT NOT NULL DEFAULT 0,pending_media_message_id BIGINT NOT NULL DEFAULT 0,last_human_reply_at TIMESTAMPTZ,updated_at TIMESTAMPTZ NOT NULL DEFAULT now());`
const FTSExpression = "to_tsvector('russian', question || ' ' || COALESCE(keywords, '') || ' ' || answer)"

// Migrate is idempotent, keeps historical UTC values, and only rebuilds vectors on dimension changes.
func (s *Store) Migrate(ctx context.Context, dimension int) error {
	if dimension < 1 || dimension > 16000 {
		return fmt.Errorf("invalid embedding dimension")
	}
	if _, e := s.Pool.Exec(ctx, "CREATE EXTENSION IF NOT EXISTS vector"); e != nil {
		return fmt.Errorf("pgvector: %w", e)
	}
	if _, e := s.Pool.Exec(ctx, tables); e != nil {
		return e
	}
	for _, c := range []string{"last_bot_reply_message_id", "last_human_reply_message_id", "last_mirrored_media_message_id", "pending_media_message_id"} {
		if _, e := s.Pool.Exec(ctx, "ALTER TABLE bedolaga_ticket_state ADD COLUMN IF NOT EXISTS "+c+" BIGINT NOT NULL DEFAULT 0"); e != nil {
			return e
		}
	}
	if _, e := s.Pool.Exec(ctx, "ALTER TABLE topic_mappings ADD COLUMN IF NOT EXISTS active_ticket_id BIGINT; ALTER TABLE bedolaga_ticket_state ADD COLUMN IF NOT EXISTS last_human_reply_at TIMESTAMPTZ"); e != nil {
		return e
	}
	for _, pair := range [][2]string{{"topic_mappings", "created_at"}, {"message_mappings", "created_at"}, {"chat_messages", "created_at"}, {"llm_token_usage", "created_at"}, {"user_names", "updated_at"}, {"knowledge_gaps", "first_seen"}, {"knowledge_gaps", "last_seen"}, {"bedolaga_ticket_state", "last_human_reply_at"}} {
		var typ string
		e := s.Pool.QueryRow(ctx, `SELECT data_type FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=$1 AND column_name=$2`, pair[0], pair[1]).Scan(&typ)
		if errors.Is(e, pgx.ErrNoRows) {
			_, e = s.Pool.Exec(ctx, "ALTER TABLE "+pair[0]+" ADD COLUMN "+pair[1]+" TIMESTAMPTZ NOT NULL DEFAULT now()")
		} else if e == nil && typ == "timestamp without time zone" {
			_, e = s.Pool.Exec(ctx, "ALTER TABLE "+pair[0]+" ALTER COLUMN "+pair[1]+" TYPE TIMESTAMPTZ USING "+pair[1]+" AT TIME ZONE 'UTC'")
		}
		if e != nil {
			return e
		}
	}
	if _, e := s.Pool.Exec(ctx, "ALTER TABLE faq DROP COLUMN IF EXISTS images; ALTER TABLE faq ADD COLUMN IF NOT EXISTS image VARCHAR(255)"); e != nil {
		return e
	}
	for _, table := range []string{"faq", "knowledge_gaps"} {
		var old int
		e := s.Pool.QueryRow(ctx, `SELECT atttypmod FROM pg_attribute WHERE attrelid=$1::regclass AND attname='embedding' AND NOT attisdropped`, table).Scan(&old)
		if e != nil && !errors.Is(e, pgx.ErrNoRows) {
			return e
		}
		if old != dimension {
			if _, e = s.Pool.Exec(ctx, "ALTER TABLE "+table+" DROP COLUMN IF EXISTS embedding; ALTER TABLE "+table+fmt.Sprintf(" ADD COLUMN embedding vector(%d)", dimension)); e != nil {
				return e
			}
		} // HNSW supports vector dimensions up to 2000, same as the existing providers.
		if _, e = s.Pool.Exec(ctx, fmt.Sprintf("CREATE INDEX IF NOT EXISTS %s_embedding_idx ON %s USING hnsw(embedding vector_cosine_ops)", table, table)); e != nil {
			return e
		}
	}
	_, e := s.Pool.Exec(ctx, "CREATE INDEX IF NOT EXISTS faq_fts_idx ON faq USING gin ("+FTSExpression+")")
	return e
}
func (s *Store) UpsertUser(ctx context.Context, u domain.User) error {
	_, e := s.Pool.Exec(ctx, `INSERT INTO user_names(telegram_id,username,first_name,last_name,updated_at) VALUES($1,NULLIF($2,''),NULLIF($3,''),NULLIF($4,''),now()) ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,last_name=excluded.last_name,updated_at=now()`, u.TelegramID, u.Username, u.FirstName, u.LastName)
	return e
}
func (s *Store) UserByID(ctx context.Context, id int64) (*domain.User, error) {
	u := &domain.User{}
	e := s.Pool.QueryRow(ctx, `SELECT telegram_id,COALESCE(username,''),COALESCE(first_name,''),COALESCE(last_name,'') FROM user_names WHERE telegram_id=$1`, id).Scan(&u.TelegramID, &u.Username, &u.FirstName, &u.LastName)
	if errors.Is(e, pgx.ErrNoRows) {
		return nil, nil
	}
	return u, e
}
func (s *Store) topic(ctx context.Context, col string, id int64) (*domain.Topic, error) {
	t := &domain.Topic{}
	e := s.Pool.QueryRow(ctx, "SELECT user_id,topic_id,COALESCE(user_name,''),active_ticket_id,created_at FROM topic_mappings WHERE "+col+"=$1", id).Scan(&t.UserID, &t.TopicID, &t.UserName, &t.ActiveTicketID, &t.CreatedAt)
	if errors.Is(e, pgx.ErrNoRows) {
		return nil, nil
	}
	return t, e
}
func (s *Store) TopicByUser(ctx context.Context, id int64) (*domain.Topic, error) {
	return s.topic(ctx, "user_id", id)
}
func (s *Store) TopicByID(ctx context.Context, id int64) (*domain.Topic, error) {
	return s.topic(ctx, "topic_id", id)
}
func (s *Store) SaveTopic(ctx context.Context, t domain.Topic) error {
	_, e := s.Pool.Exec(ctx, `INSERT INTO topic_mappings(user_id,topic_id,user_name,active_ticket_id,created_at) VALUES($1,$2,NULLIF($3,''),$4,now()) ON CONFLICT(user_id) DO UPDATE SET topic_id=excluded.topic_id,user_name=excluded.user_name,active_ticket_id=excluded.active_ticket_id`, t.UserID, t.TopicID, t.UserName, t.ActiveTicketID)
	return e
}
func (s *Store) DeleteTopic(ctx context.Context, user, topic int64) error {
	_, e := s.Pool.Exec(ctx, "DELETE FROM topic_mappings WHERE user_id=$1 AND topic_id=$2", user, topic)
	return e
}
func (s *Store) SetActiveTicket(ctx context.Context, user int64, id *int64) error {
	_, e := s.Pool.Exec(ctx, "UPDATE topic_mappings SET active_ticket_id=$2 WHERE user_id=$1", user, id)
	return e
}
func (s *Store) SaveMessageMapping(ctx context.Context, m domain.MessageMapping) error {
	_, e := s.Pool.Exec(ctx, `INSERT INTO message_mappings(topic_message_id,topic_id,user_chat_id,user_message_id,created_at) VALUES($1,$2,$3,$4,now())`, m.TopicMessageID, m.TopicID, m.UserChatID, m.UserMessageID)
	return e
}
func (s *Store) mapping(ctx context.Context, cond string, a, b int64) (*domain.MessageMapping, error) {
	m := &domain.MessageMapping{}
	e := s.Pool.QueryRow(ctx, "SELECT id,topic_message_id,topic_id,user_chat_id,user_message_id,created_at FROM message_mappings WHERE "+cond+" ORDER BY id DESC LIMIT 1", a, b).Scan(&m.ID, &m.TopicMessageID, &m.TopicID, &m.UserChatID, &m.UserMessageID, &m.CreatedAt)
	if errors.Is(e, pgx.ErrNoRows) {
		return nil, nil
	}
	return m, e
}
func (s *Store) MappingByTopicMessage(ctx context.Context, topic, msg int64) (*domain.MessageMapping, error) {
	if topic == 0 {
		return s.mapping(ctx, "($1::bigint=0) AND topic_message_id=$2", topic, msg)
	}
	return s.mapping(ctx, "topic_id=$1 AND topic_message_id=$2", topic, msg)
}
func (s *Store) MappingByUserMessage(ctx context.Context, user, msg int64) (*domain.MessageMapping, error) {
	return s.mapping(ctx, "user_chat_id=$1 AND user_message_id=$2", user, msg)
}
func (s *Store) TicketProgress(ctx context.Context, id int64) (domain.TicketProgress, error) {
	p := domain.TicketProgress{}
	e := s.Pool.QueryRow(ctx, `SELECT last_answered_message_id,COALESCE(last_bot_reply_message_id,0),COALESCE(last_human_reply_message_id,0),COALESCE(last_mirrored_media_message_id,0),COALESCE(pending_media_message_id,0),last_human_reply_at FROM bedolaga_ticket_state WHERE ticket_id=$1`, id).Scan(&p.LastAnsweredMessageID, &p.LastBotReplyMessageID, &p.LastHumanReplyMessageID, &p.LastMirroredMediaMessageID, &p.PendingMediaMessageID, &p.LastHumanReplyAt)
	if errors.Is(e, pgx.ErrNoRows) {
		e = nil
	}
	return p, e
}
func (s *Store) PendingMediaTicketIDs(ctx context.Context, limit int) ([]int64, error) {
	r, e := s.Pool.Query(ctx, "SELECT ticket_id FROM bedolaga_ticket_state WHERE pending_media_message_id>last_mirrored_media_message_id LIMIT $1", limit)
	if e != nil {
		return nil, e
	}
	defer r.Close()
	out := []int64{}
	for r.Next() {
		var x int64
		if e = r.Scan(&x); e != nil {
			return nil, e
		}
		out = append(out, x)
	}
	return out, r.Err()
}
func (s *Store) ticketUpsert(ctx context.Context, id int64, set string, args ...any) error {
	tx, e := s.Pool.Begin(ctx)
	if e != nil {
		return e
	}
	defer tx.Rollback(ctx)
	if _, e = tx.Exec(ctx, `INSERT INTO bedolaga_ticket_state(ticket_id,last_answered_message_id,updated_at) VALUES($1,0,now()) ON CONFLICT DO NOTHING`, id); e != nil {
		return e
	}
	if _, e = tx.Exec(ctx, "UPDATE bedolaga_ticket_state SET "+set+",updated_at=now() WHERE ticket_id=$1", append([]any{id}, args...)...); e != nil {
		return e
	}
	return tx.Commit(ctx)
}
func (s *Store) RecordPendingMedia(ctx context.Context, id, msg int64) error {
	return s.ticketUpsert(ctx, id, "pending_media_message_id=GREATEST(pending_media_message_id,$2)", msg)
}
func (s *Store) RecordMirroredMedia(ctx context.Context, id, msg int64) error {
	return s.ticketUpsert(ctx, id, "last_mirrored_media_message_id=GREATEST(last_mirrored_media_message_id,$2),pending_media_message_id=CASE WHEN pending_media_message_id<=GREATEST(last_mirrored_media_message_id,$2) THEN 0 ELSE pending_media_message_id END", msg)
}
func (s *Store) RecordHumanReply(ctx context.Context, id, msg int64, at time.Time) error {
	if at.IsZero() {
		at = time.Now().UTC()
	}
	return s.ticketUpsert(ctx, id, "last_human_reply_message_id=GREATEST(last_human_reply_message_id,$2),last_human_reply_at=$3", msg, at)
}
func (s *Store) RecordTicketReply(ctx context.Context, id int64, bot, answered *int64) error {
	return s.ticketUpsert(ctx, id, "last_bot_reply_message_id=COALESCE($2,last_bot_reply_message_id),last_answered_message_id=COALESCE($3,last_answered_message_id)", bot, answered)
}
func (s *Store) SaveUsage(ctx context.Context, id int64, u domain.TokenUsage) error {
	_, e := s.Pool.Exec(ctx, `INSERT INTO llm_token_usage(telegram_id,prompt_tokens,completion_tokens,total_tokens,created_at) VALUES($1,$2,$3,$4,now())`, id, u.PromptTokens, u.CompletionTokens, u.TotalTokens)
	return e
}
func (s *Store) Stats(ctx context.Context) (domain.Stats, error) {
	v := domain.Stats{}
	e := s.Pool.QueryRow(ctx, `SELECT COUNT(*),COUNT(DISTINCT telegram_id),COALESCE(SUM(prompt_tokens),0),COALESCE(SUM(completion_tokens),0),COALESCE(SUM(total_tokens),0) FROM llm_token_usage`).Scan(&v.Requests, &v.Users, &v.PromptTokens, &v.CompletionTokens, &v.TotalTokens)
	return v, e
}
func (s *Store) UsageTop(ctx context.Context, limit int) ([]domain.UserUsage, error) {
	r, e := s.Pool.Query(ctx, `SELECT telegram_id,COALESCE(SUM(prompt_tokens),0),COALESCE(SUM(completion_tokens),0),COALESCE(SUM(total_tokens),0),COUNT(*) FROM llm_token_usage WHERE telegram_id IS NOT NULL GROUP BY telegram_id ORDER BY SUM(total_tokens) DESC NULLS LAST LIMIT $1`, limit)
	if e != nil {
		return nil, e
	}
	defer r.Close()
	out := []domain.UserUsage{}
	for r.Next() {
		var u domain.UserUsage
		if e = r.Scan(&u.TelegramID, &u.PromptTokens, &u.CompletionTokens, &u.TotalTokens, &u.RequestCount); e != nil {
			return nil, e
		}
		out = append(out, u)
	}
	return out, r.Err()
}
func (s *Store) UsageForUser(ctx context.Context, id int64) (*domain.UserUsage, error) {
	u := &domain.UserUsage{TelegramID: id}
	e := s.Pool.QueryRow(ctx, `SELECT COALESCE(SUM(prompt_tokens),0),COALESCE(SUM(completion_tokens),0),COALESCE(SUM(total_tokens),0),COUNT(*) FROM llm_token_usage WHERE telegram_id=$1`, id).Scan(&u.PromptTokens, &u.CompletionTokens, &u.TotalTokens, &u.RequestCount)
	return u, e
}
func (s *Store) ClearUsage(ctx context.Context) (int64, error) {
	r, e := s.Pool.Exec(ctx, "DELETE FROM llm_token_usage")
	return r.RowsAffected(), e
}
func (s *Store) PurgeMessageMappings(ctx context.Context, before time.Time) (int64, error) {
	r, e := s.Pool.Exec(ctx, "DELETE FROM message_mappings WHERE created_at<$1", before)
	return r.RowsAffected(), e
}
