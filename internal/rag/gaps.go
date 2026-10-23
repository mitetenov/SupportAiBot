package rag

import (
	"context"
	"errors"
	"strings"
	"supportbot/internal/domain"
	"supportbot/internal/storage"

	"github.com/jackc/pgx/v5"
)

type Gaps struct {
	store     *storage.Store
	faq       *Service
	embedding Embedding
}

func NewGaps(s *storage.Store, f *Service, e Embedding) *Gaps { return &Gaps{s, f, e} }
func DetermineTrigger(response string, similarity float64, best string) string {
	if best == "" && similarity == 0 {
		return "NO_MATCH"
	}
	if strings.Contains(response, "[ESCALATE]") {
		return "ESCALATED"
	}
	if similarity > 0 && similarity < 0.72 {
		return "LOW_SIMILARITY"
	}
	if hasAny(response, []string{"не знаю", "не могу ответить", "не могу помочь", "затрудняюсь ответить", "не обладаю информацией"}) {
		return "LLM_UNSURE"
	}
	return ""
}
func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}
func (g *Gaps) Evaluate(ctx context.Context, q string, id int64, response string, f domain.FAQContext) error {
	if strings.TrimSpace(q) == "" {
		return nil
	}
	reason := DetermineTrigger(response, f.MaxSimilarity, f.BestQuestion)
	if reason == "" {
		return nil
	}
	return g.save(ctx, q, id, response, f, reason)
}
func (g *Gaps) EvaluateOperatorRequest(ctx context.Context, q string, id int64, f domain.FAQContext) error {
	if strings.TrimSpace(q) == "" {
		return nil
	}
	return g.save(ctx, q, id, "[Пользователь запросил оператора после ответа бота]", f, "USER_OPERATOR")
}
func (g *Gaps) save(ctx context.Context, q string, id int64, response string, f domain.FAQContext, reason string) error {
	q = truncate(strings.TrimSpace(q), 2000)
	v, _ := g.faq.EmbedQuery(ctx, q)
	var existing int64
	var e error
	var vector any
	if len(v) > 0 {
		vector = VectorString(v)
		var similarity float64
		e = g.store.Pool.QueryRow(ctx, "SELECT id,1-(embedding<=>$1::vector) FROM knowledge_gaps WHERE embedding IS NOT NULL ORDER BY embedding<=>$1::vector LIMIT 1", vector).Scan(&existing, &similarity)
		if similarity < 0.85 {
			existing = 0
		}
	} else {
		e = g.store.Pool.QueryRow(ctx, "SELECT id FROM knowledge_gaps WHERE user_query=$1 ORDER BY id LIMIT 1", q).Scan(&existing)
	}
	if e != nil && !errors.Is(e, pgx.ErrNoRows) {
		return e
	}
	if existing != 0 {
		_, e = g.store.Pool.Exec(ctx, "UPDATE knowledge_gaps SET gap_count=gap_count+1,last_seen=now() WHERE id=$1", existing)
		return e
	}
	_, e = g.store.Pool.Exec(ctx, `INSERT INTO knowledge_gaps(user_query,embedding,telegram_id,best_faq_question,max_similarity,faq_count,trigger_reason,bot_response,gap_count,first_seen,last_seen) VALUES($1,$2::vector,$3,NULLIF($4,''),$5,$6,$7,NULLIF($8,''),1,now(),now())`, q, vector, id, f.BestQuestion, f.MaxSimilarity, len(f.Results), reason, truncate(response, 500))
	return e
}
func (g *Gaps) Top(ctx context.Context, limit int) ([]domain.Gap, error) {
	limit = max(1, min(100, limit))
	r, e := g.store.Pool.Query(ctx, `SELECT id,user_query,gap_count,COALESCE(trigger_reason,''),first_seen,last_seen FROM knowledge_gaps ORDER BY gap_count DESC,last_seen DESC LIMIT $1`, limit)
	if e != nil {
		return nil, e
	}
	defer r.Close()
	out := []domain.Gap{}
	for r.Next() {
		var x domain.Gap
		if e = r.Scan(&x.ID, &x.UserQuery, &x.GapCount, &x.TriggerReason, &x.FirstSeen, &x.LastSeen); e != nil {
			return nil, e
		}
		out = append(out, x)
	}
	return out, r.Err()
}
func (g *Gaps) Clear(ctx context.Context) (int64, error) {
	r, e := g.store.Pool.Exec(ctx, "DELETE FROM knowledge_gaps")
	return r.RowsAffected(), e
}
