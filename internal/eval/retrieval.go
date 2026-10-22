package eval

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"sort"
	"strings"
	"supportbot/internal/domain"
	"supportbot/internal/rag"
	"supportbot/internal/storage"
	"sync"

	"github.com/jackc/pgx/v5"
)

func ValidateDatabaseURL(dsn string) error {
	u, e := url.Parse(strings.Replace(dsn, "postgresql+asyncpg:", "postgresql:", 1))
	if e != nil {
		return fmt.Errorf("use a disposable loopback PostgreSQL database named prompt_opt*")
	}
	if (u.Scheme != "postgres" && u.Scheme != "postgresql") || (u.Hostname() != "127.0.0.1" && u.Hostname() != "localhost" && u.Hostname() != "::1") || !strings.HasPrefix(strings.TrimPrefix(u.Path, "/"), "prompt_opt") {
		return fmt.Errorf("use a disposable loopback PostgreSQL database named prompt_opt*")
	}
	cfg, err := pgx.ParseConfig(strings.Replace(dsn, "postgresql+asyncpg:", "postgresql:", 1))
	if err != nil || (cfg.Host != "127.0.0.1" && cfg.Host != "localhost" && cfg.Host != "::1") || !strings.HasPrefix(cfg.Database, "prompt_opt") {
		return fmt.Errorf("use a disposable loopback PostgreSQL database named prompt_opt*")
	}
	for _, fallback := range cfg.Fallbacks {
		if fallback.Host != "127.0.0.1" && fallback.Host != "localhost" && fallback.Host != "::1" {
			return fmt.Errorf("database fallback must be loopback")
		}
	}
	return nil
}

type cachedEmbedding struct {
	rag.Embedding
	mu     sync.Mutex
	values map[string][]float64
}

func CachedEmbedding(e rag.Embedding) rag.Embedding {
	return &cachedEmbedding{Embedding: e, values: map[string][]float64{}}
}
func (c *cachedEmbedding) Embed(ctx context.Context, s string) ([]float64, error) {
	v, e := c.EmbedBatch(ctx, []string{s})
	if e != nil {
		return nil, e
	}
	return v[0], nil
}
func (c *cachedEmbedding) EmbedBatch(ctx context.Context, texts []string) ([][]float64, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	missing := []string{}
	for _, s := range texts {
		if _, ok := c.values[s]; !ok && !contains(missing, s) {
			missing = append(missing, s)
		}
	}
	if len(missing) > 0 {
		vectors, e := c.Embedding.EmbedBatch(ctx, missing)
		if e != nil {
			return nil, e
		}
		if len(vectors) != len(missing) {
			return nil, fmt.Errorf("incomplete embedding comparison")
		}
		for i, v := range vectors {
			if len(v) != c.Dimension() {
				return nil, fmt.Errorf("invalid embedding dimensions")
			}
			c.values[missing[i]] = append([]float64{}, v...)
		}
	}
	out := make([][]float64, len(texts))
	for i, s := range texts {
		out[i] = append([]float64{}, c.values[s]...)
	}
	return out, nil
}

type QueryCase struct {
	Query    string   `json:"query"`
	Source   string   `json:"source"`
	Expected []string `json:"expected"`
	TunedFor bool     `json:"tuned_for"`
}
type RetrievalRow struct {
	QueryCase
	Titles       []string `json:"titles"`
	RawTitles    []string `json:"raw_titles"`
	ContextChars int      `json:"context_chars"`
	Hit          bool     `json:"hit"`
	Top1         bool     `json:"top1"`
}
type RetrievalScore struct {
	HitAt1              int `json:"hit_at_1"`
	HitAtK              int `json:"hit_at_k"`
	Missed              int `json:"missed"`
	ControlFalseMatches int `json:"control_false_matches"`
	Positives           int `json:"positives"`
	Controls            int `json:"controls"`
	HeldHitAt1          int `json:"held_hit_at_1"`
	HeldHitAtK          int `json:"held_hit_at_k"`
	HeldPositives       int `json:"held_positives"`
}

func (s *RetrievalScore) Add(c QueryCase, titles []string) {
	if len(c.Expected) == 0 {
		s.Controls++
		if len(titles) > 0 {
			s.ControlFalseMatches++
		}
		return
	}
	s.Positives++
	first := len(titles) > 0 && contains(c.Expected, titles[0])
	hit := false
	for _, title := range titles {
		hit = hit || contains(c.Expected, title)
	}
	if first {
		s.HitAt1++
	}
	if hit {
		s.HitAtK++
	} else {
		s.Missed++
	}
	if !c.TunedFor {
		s.HeldPositives++
		if first {
			s.HeldHitAt1++
		}
		if hit {
			s.HeldHitAtK++
		}
	}
}
func LoadQueries(path string) ([]QueryCase, error) {
	raw, e := os.ReadFile(path)
	if e != nil {
		return nil, e
	}
	var p struct {
		Cases []QueryCase `json:"cases"`
	}
	if e = json.Unmarshal(raw, &p); e != nil {
		return nil, e
	}
	if len(p.Cases) == 0 {
		return nil, fmt.Errorf("no retrieval cases")
	}
	return p.Cases, nil
}
func HashFile(path string) (string, error) {
	raw, e := os.ReadFile(path)
	if e != nil {
		return "", e
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

type Retriever struct {
	Service   *rag.Service
	Entries   []fixtureArticle
	Store     *storage.Store
	Reindexed bool
}

func NewRetriever(ctx context.Context, store *storage.Store, embedding rag.Embedding, faqPath string, reindex bool) (*Retriever, error) {
	r := &Retriever{Service: rag.New(store, embedding), Store: store, Reindexed: reindex}
	raw, e := os.ReadFile(faqPath)
	if e != nil {
		return nil, e
	}
	if e = json.Unmarshal(raw, &r.Entries); e != nil {
		return nil, e
	}
	if reindex {
		if e = r.Service.Initialize(ctx, faqPath); e != nil {
			return nil, e
		}
	} else {
		if e = store.Migrate(ctx, embedding.Dimension()); e != nil {
			return nil, e
		}
		r.Service.SetVectorSearchEnabled(true)
		r.Service.MarkReady()
	}
	return r, nil
}
func (r *Retriever) search(ctx context.Context, q string, minSim float64, limit int) ([]domain.FAQResult, error) {
	vector, e := r.Service.EmbedQuery(ctx, q)
	if e != nil || len(vector) == 0 {
		return nil, fmt.Errorf("embedding failed; retrieval measurement incomplete")
	}
	rows, e := r.Store.Pool.Query(ctx, rag.HYBRID_SEARCH_SQL, pgx.NamedArgs{"vector_str": rag.VectorString(vector), "clean_query": rag.CleanQuery(q), "excluded": []string{}, "limit": limit, "candidates": 20, "rrf_k": 60, "min_vector_sim": minSim, "min_fts_rank": 0.01})
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []domain.FAQResult{}
	for rows.Next() {
		var x domain.FAQResult
		var image *string
		var rank float64
		if e = rows.Scan(&x.Question, &x.Answer, &image, &x.Similarity, &rank, &x.RRFScore); e != nil {
			return nil, e
		}
		if image != nil {
			x.Image = *image
		}
		out = append(out, x)
	}
	return out, rows.Err()
}
func FallbackQueries(q string) []string {
	queries := []string{q}
	lower := strings.ToLower(q)
	if anyText(lower, []string{"подключ", "не работ", "не заход", "vpn", "впн", "скорост", "медлен", "сайт", "instagram", "ошибк", "отвали", "обрыв", "обнов", "подписк", "пинг", "сервер"}) {
		queries = append(queries, rag.ConnectionFAQQuery)
	}
	if anyText(lower, []string{"реферал", "партнёр", "партнер", "partner", "приглас", "приглаш", "друг", "друз", "бонус"}) {
		queries = append(queries, rag.ReferralFAQQuery)
	}
	return queries
}
func MergeResults(groups [][]domain.FAQResult, mode string) []domain.FAQResult {
	out := []domain.FAQResult{}
	indices := map[string]int{}
	if mode == "off" && len(groups) > 1 {
		groups = groups[:1]
	}
	for _, group := range groups {
		group = append([]domain.FAQResult{}, group...)
		sort.SliceStable(group, func(i, j int) bool { return group[i].RRFScore > group[j].RRFScore })
		for _, hit := range group {
			if i, ok := indices[hit.Question]; ok {
				if mode == "compete" && hit.RRFScore > out[i].RRFScore {
					out[i] = hit
				}
				continue
			}
			indices[hit.Question] = len(out)
			out = append(out, hit)
		}
	}
	if mode == "compete" {
		sort.SliceStable(out, func(i, j int) bool { return out[i].RRFScore > out[j].RRFScore })
	}
	return out[:min(5, len(out))]
}
func (r *Retriever) Retrieve(ctx context.Context, q string, minSim float64, limit int, mode string, catalog bool) ([]domain.FAQResult, []domain.FAQResult, error) {
	if mode != "append" && mode != "compete" && mode != "off" {
		return nil, nil, fmt.Errorf("invalid fallback mode")
	}
	if minSim < 0 || minSim > 1 || limit < 1 {
		return nil, nil, fmt.Errorf("invalid retrieval thresholds")
	}
	queries := FallbackQueries(q)
	if mode == "off" {
		queries = queries[:1]
	}
	groups := make([][]domain.FAQResult, len(queries))
	for i, query := range queries {
		var e error
		groups[i], e = r.search(ctx, query, minSim, limit)
		if e != nil {
			return nil, nil, e
		}
	}
	raw := MergeResults(groups, mode)
	selected := append([]domain.FAQResult{}, raw...)
	if catalog {
		for _, id := range rag.RequiredIDs(q) {
			for _, entry := range r.Entries {
				entryID := entry.ID
				if entryID == "" && entry.Question == "Как поделиться подпиской с членом семьи?" {
					entryID = "subscription_sharing"
				}
				if entryID != id {
					continue
				}
				found := false
				for _, hit := range selected {
					found = found || hit.Question == entry.Question
				}
				if !found {
					selected = append(selected, domain.FAQResult{Question: entry.Question, Answer: entry.Answer, Image: entry.Image})
				}
			}
		}
	}
	return raw, selected, nil
}
func Titles(results []domain.FAQResult) []string {
	out := []string{}
	for _, r := range results {
		out = append(out, r.Question)
	}
	return out
}
func BaselineContext(results []domain.FAQResult) string {
	if len(results) == 0 {
		return ""
	}
	s := "Кандидаты FAQ (проверь соответствие вопросу, истории и фактам инструментов; разрешено кратко изложить подходящую часть с сохранением точных названий, ограничений, условий и порядка шагов; если кандидаты не подходят, уточни проблему, не давай нерелевантных инструкций):\n"
	for _, r := range results {
		s += "Вопрос: " + r.Question + "\nИнструкция: " + r.Answer + "\n\n"
	}
	return s
}
