// Package rag implements the original hybrid PostgreSQL/vector retrieval and FAQ indexing.
package rag

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/jackc/pgx/v5"
	"os"
	"regexp"
	"sort"
	"strings"
	"supportbot/internal/diagnostics"
	"supportbot/internal/domain"
	"supportbot/internal/storage"
	"sync"
	"time"
	"unicode"
)

const ConnectionFAQQuery = "Не могу подключиться к VPN / не работает / не заходит"
const ReferralFAQQuery = "Реферальная программа, партнёрка, реферальная ссылка, пригласить друга, бонусы, рефералы"
const sharingFAQQuestion = "Как поделиться подпиской с членом семьи?"

type Service struct {
	Store                *storage.Store
	Embedding            Embedding
	mu                   sync.Mutex
	ready, vectorEnabled bool
	cache                map[string][]float64
	cacheOrder           []string
	catalog              []catalogEntry
}
type catalogEntry struct {
	ID       string          `json:"id"`
	Question string          `json:"question"`
	Answer   string          `json:"answer"`
	Keywords json.RawMessage `json:"keywords"`
	Image    string          `json:"image"`
}

func New(s *storage.Store, e Embedding) *Service {
	return &Service{Store: s, Embedding: e, cache: map[string][]float64{}}
}
func (s *Service) IsReady() bool { s.mu.Lock(); defer s.mu.Unlock(); return s.ready }
func (s *Service) SetVectorSearchEnabled(enabled bool) {
	s.mu.Lock()
	s.vectorEnabled = enabled
	s.mu.Unlock()
}
func (s *Service) MarkReady() { s.mu.Lock(); s.ready = true; s.mu.Unlock() }
func (s *Service) GetIndexFingerprint(hash string) string {
	data := s.Embedding.Fingerprint()
	data["faq_hash"] = hash
	data["preparation"] = "faq-document-v2"
	b, _ := json.Marshal(data)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
func (s *Service) EmbedQuery(ctx context.Context, text string) ([]float64, error) {
	if strings.TrimSpace(text) == "" {
		return nil, nil
	}
	s.mu.Lock()
	if v, ok := s.cache[text]; ok {
		s.touchCacheLocked(text)
		s.mu.Unlock()
		return append([]float64{}, v...), nil
	}
	s.mu.Unlock()
	v, e := s.Embedding.Embed(ctx, text)
	if e != nil || len(v) == 0 {
		return nil, e
	}
	if len(v) != s.Embedding.Dimension() {
		return nil, fmt.Errorf("embedding dimension mismatch")
	}
	s.mu.Lock()
	s.removeCacheOrderLocked(text)
	if len(s.cacheOrder) >= 256 {
		delete(s.cache, s.cacheOrder[0])
		s.cacheOrder = s.cacheOrder[1:]
	}
	s.cache[text] = append([]float64{}, v...)
	s.cacheOrder = append(s.cacheOrder, text)
	s.mu.Unlock()
	return v, nil
}
func (s *Service) removeCacheOrderLocked(key string) {
	for i, k := range s.cacheOrder {
		if k == key {
			s.cacheOrder = append(s.cacheOrder[:i], s.cacheOrder[i+1:]...)
			return
		}
	}
}
func (s *Service) touchCacheLocked(key string) {
	s.removeCacheOrderLocked(key)
	s.cacheOrder = append(s.cacheOrder, key)
}
func (s *Service) Initialize(ctx context.Context, path string) error {
	diagnostics.Info(ctx, "FAQ initialization started", "embedding_dimension", s.Embedding.Dimension())
	s.SetVectorSearchEnabled(false)
	raw, e := os.ReadFile(path)
	if os.IsNotExist(e) {
		s.MarkReady()
		return nil
	}
	if e != nil {
		return e
	}
	if e = json.Unmarshal(raw, &s.catalog); e != nil {
		return e
	}
	seen := map[string]bool{}
	for i := range s.catalog {
		// Give this legacy article an in-memory anchor without changing the source
		// JSON hash or rebuilding already compatible persisted embeddings.
		if s.catalog[i].ID == "" && s.catalog[i].Question == sharingFAQQuestion {
			s.catalog[i].ID = "subscription_sharing"
		}
		r := s.catalog[i]
		if r.ID != "" && seen[r.ID] {
			return fmt.Errorf("duplicate FAQ id")
		}
		seen[r.ID] = true
	}
	if e = s.Store.Migrate(ctx, s.Embedding.Dimension()); e != nil {
		return e
	}
	hash := sha256.Sum256(raw)
	fingerprint := s.GetIndexFingerprint(hex.EncodeToString(hash[:]))
	var old string
	_ = s.Store.Pool.QueryRow(ctx, "SELECT val FROM faq_metadata WHERE key='faq_index_fingerprint'").Scan(&old)
	var count, indexed int
	e = s.Store.Pool.QueryRow(ctx, "SELECT COUNT(*),COUNT(embedding) FROM faq").Scan(&count, &indexed)
	if e != nil {
		return e
	}
	if old == fingerprint && count > 0 && count == indexed {
		diagnostics.Info(ctx, "FAQ index unchanged; retained embeddings", "indexed_count", indexed)
		s.SetVectorSearchEnabled(true)
		s.MarkReady()
		return nil
	}
	entries := []domain.FAQEntry{}
	for _, r := range s.catalog {
		if r.Question == "" || r.Answer == "" {
			continue
		}
		entries = append(entries, domain.FAQEntry{Question: r.Question, Answer: r.Answer, Keywords: parseKeywords(r.Keywords), Image: strings.TrimSpace(r.Image)})
	}
	diagnostics.Info(ctx, "FAQ reindex started", "expected_count", len(entries))
	n, e := s.ReplaceFAQBatch(ctx, entries)
	if e != nil {
		diagnostics.Error(ctx, "FAQ replacement failed; retained text search", e)
		s.MarkReady()
		return e
	}
	if n == len(entries) {
		_, e = s.Store.Pool.Exec(ctx, "INSERT INTO faq_metadata(key,val) VALUES('faq_index_fingerprint',$1) ON CONFLICT(key) DO UPDATE SET val=excluded.val", fingerprint)
		if e == nil {
			s.SetVectorSearchEnabled(true)
		}
	}
	diagnostics.Info(ctx, "FAQ initialization completed", "indexed_count", n, "expected_count", len(entries))
	s.MarkReady()
	return e
}
func parseKeywords(raw json.RawMessage) string {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return strings.TrimSpace(s)
	}
	var items []any
	if json.Unmarshal(raw, &items) != nil {
		return ""
	}
	out := []string{}
	for _, x := range items {
		v := strings.TrimSpace(fmt.Sprint(x))
		if v != "" {
			out = append(out, v)
		}
	}
	return strings.Join(out, ", ")
}
func WithGlobalAliases(keywords string) string {
	out := []string{}
	if strings.TrimSpace(keywords) != "" {
		out = append(out, strings.TrimSpace(keywords))
	}
	out = append(out, "vpn", "впн", "вэпэн")
	return strings.Join(out, ", ")
}
func EmbedText(question, answer, keywords string) string {
	return question + " " + keywords + "\n" + answer
}
func UUID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = (b[6] & 15) | 64
	b[8] = (b[8] & 63) | 128
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
}
func (s *Service) ReplaceFAQBatch(ctx context.Context, entries []domain.FAQEntry) (int, error) {
	vectors := make([][]float64, len(entries))
	for start := 0; start < len(entries); start += 32 {
		end := min(start+32, len(entries))
		texts := []string{}
		for _, r := range entries[start:end] {
			texts = append(texts, EmbedText(r.Question, r.Answer, WithGlobalAliases(r.Keywords)))
		}
		v, e := s.Embedding.EmbedBatch(ctx, texts)
		if e != nil {
			return 0, e
		}
		if len(v) != len(texts) {
			return 0, fmt.Errorf("partial FAQ batch")
		}
		for i, x := range v {
			if len(x) != s.Embedding.Dimension() {
				return 0, fmt.Errorf("FAQ vector dimension mismatch")
			}
			vectors[start+i] = x
		}
	}
	tx, e := s.Store.Pool.Begin(ctx)
	if e != nil {
		return 0, e
	}
	defer tx.Rollback(ctx)
	if _, e = tx.Exec(ctx, "DELETE FROM faq"); e != nil {
		return 0, e
	}
	for i, r := range entries {
		_, e = tx.Exec(ctx, "INSERT INTO faq(id,question,answer,keywords,image,embedding) VALUES($1,$2,$3,$4,NULLIF($5,''),$6::vector)", UUID(), r.Question, r.Answer, WithGlobalAliases(r.Keywords), r.Image, VectorString(vectors[i]))
		if e != nil {
			return 0, e
		}
	}
	return len(entries), tx.Commit(ctx)
}
func excludedList(exclude map[string]bool) []string {
	out := []string{}
	for q, v := range exclude {
		if v {
			out = append(out, q)
		}
	}
	sort.Strings(out)
	return out
}

var cleanRE = regexp.MustCompile(`[^a-zA-Zа-яА-Я0-9\s]`)

func CleanQuery(q string) string {
	clean := strings.TrimSpace(cleanRE.ReplaceAllString(q, " "))
	if clean == "" {
		return q
	}
	return clean
}
func (s *Service) Search(ctx context.Context, query string, exclude map[string]bool) ([]domain.FAQResult, error) {
	started := time.Now()
	defer func() {
		diagnostics.Info(ctx, "FAQ search completed", "duration_seconds", time.Since(started).Seconds())
	}()
	diagnostics.Payload(ctx, "FAQ search query", map[string]any{"query": query, "excluded": excludedList(exclude)})
	s.mu.Lock()
	enabled, ready := s.vectorEnabled, s.ready
	s.mu.Unlock()
	if !ready || strings.TrimSpace(query) == "" {
		return nil, nil
	}
	if !enabled {
		diagnostics.Info(ctx, "FAQ search uses retained full-text index")
		return s.fts(ctx, query, exclude)
	}
	v, e := s.EmbedQuery(ctx, query)
	if e != nil || len(v) == 0 {
		diagnostics.Info(ctx, "FAQ embedding unavailable; degrading to full-text search")
		return s.fts(ctx, query, exclude)
	}
	args := pgx.NamedArgs{"vector_str": VectorString(v), "clean_query": CleanQuery(query), "excluded": excludedList(exclude), "limit": 3, "candidates": 20, "rrf_k": 60, "min_vector_sim": 0.45, "min_fts_rank": 0.01}
	r, e := s.Store.Pool.Query(ctx, HYBRID_SEARCH_SQL, args)
	if e != nil {
		diagnostics.Error(ctx, "FAQ hybrid search failed; degrading to vector", e)
		return s.vector(ctx, v, exclude)
	}
	defer r.Close()
	out := []domain.FAQResult{}
	for r.Next() {
		var x domain.FAQResult
		var image *string
		var rank float64
		if e = r.Scan(&x.Question, &x.Answer, &image, &x.Similarity, &rank, &x.RRFScore); e != nil {
			return nil, e
		}
		if image != nil {
			x.Image = *image
		}
		out = append(out, x)
	}
	diagnostics.Payload(ctx, "FAQ search candidates", out)
	return out, r.Err()
}
func (s *Service) fts(ctx context.Context, q string, exclude map[string]bool) ([]domain.FAQResult, error) {
	r, e := s.Store.Pool.Query(ctx, FTS_SEARCH_SQL, pgx.NamedArgs{"clean_query": CleanQuery(q), "excluded": excludedList(exclude), "limit": 3})
	if e != nil {
		return nil, e
	}
	defer r.Close()
	out := []domain.FAQResult{}
	for r.Next() {
		var x domain.FAQResult
		var image *string
		if e = r.Scan(&x.Question, &x.Answer, &image, &x.RRFScore); e != nil {
			return nil, e
		}
		if image != nil {
			x.Image = *image
		}
		if x.RRFScore >= 0.01 {
			out = append(out, x)
		}
	}
	diagnostics.Payload(ctx, "FAQ search candidates", out)
	return out, r.Err()
}
func (s *Service) vector(ctx context.Context, v []float64, exclude map[string]bool) ([]domain.FAQResult, error) {
	r, e := s.Store.Pool.Query(ctx, VECTOR_SEARCH_SQL, pgx.NamedArgs{"vector_str": VectorString(v), "excluded": excludedList(exclude), "limit": 3})
	if e != nil {
		return nil, e
	}
	defer r.Close()
	out := []domain.FAQResult{}
	for r.Next() {
		var x domain.FAQResult
		var image *string
		if e = r.Scan(&x.Question, &x.Answer, &image, &x.Similarity); e != nil {
			return nil, e
		}
		if image != nil {
			x.Image = *image
		}
		x.RRFScore = x.Similarity
		if x.Similarity >= 0.45 {
			out = append(out, x)
		}
	}
	diagnostics.Payload(ctx, "FAQ search candidates", out)
	return out, r.Err()
}
func hasAny(text string, keywords []string) bool {
	for _, k := range keywords {
		if strings.Contains(strings.ToLower(text), k) {
			return true
		}
	}
	return false
}
func (s *Service) BuildFAQContext(ctx context.Context, q string, exclude map[string]bool) (domain.FAQContext, error) {
	queries := []string{q}
	if hasAny(q, []string{"подключ", "не работ", "не заход", "vpn", "впн", "скорост", "медлен", "сайт", "instagram", "ошибк", "отвали", "обрыв", "обнов", "подписк", "пинг", "сервер"}) {
		queries = append(queries, ConnectionFAQQuery)
	}
	if hasAny(q, []string{"реферал", "партнёр", "партнер", "partner", "приглас", "приглаш", "друг", "друз", "бонус"}) {
		queries = append(queries, ReferralFAQQuery)
	}
	groups := make([][]domain.FAQResult, len(queries))
	var wg sync.WaitGroup
	for i, query := range queries {
		wg.Add(1)
		go func(i int, query string) { defer wg.Done(); groups[i], _ = s.Search(ctx, query, exclude) }(i, query)
	}
	wg.Wait()
	hits := []domain.FAQResult{}
	seen := map[string]bool{}
	for _, group := range groups {
		sort.SliceStable(group, func(i, j int) bool { return group[i].RRFScore > group[j].RRFScore })
		for _, r := range group {
			if !seen[r.Question] && !exclude[r.Question] && len(hits) < 5 {
				seen[r.Question] = true
				hits = append(hits, r)
			}
		}
	}
	for _, id := range RequiredIDs(q) {
		for _, r := range s.catalog {
			if r.ID == id && !seen[r.Question] && !exclude[r.Question] {
				seen[r.Question] = true
				hits = append(hits, domain.FAQResult{Question: r.Question, Answer: r.Answer, Image: r.Image})
				break
			}
		}
	}
	out := domain.FAQContext{Results: hits}
	if len(hits) > 0 {
		out.Text = FormatFAQContext(hits)
		out.BestQuestion = hits[0].Question
		for _, r := range hits {
			out.MaxSimilarity = max(out.MaxSimilarity, r.Similarity)
		}
	}
	return out, nil
}
func RequiredIDs(q string) []string {
	text := strings.ToLower(q)
	ids := []string{}
	matches := func(p string) bool { return regexp.MustCompile(p).MatchString(text) }
	if matches(`автопрод|автоплат|продлен[\p{L}\p{N}_]*.*(баланс|сч[её]т)`) {
		ids = append(ids, "auto_renewal")
	}
	if matches(`триал|пробн|на пробу`) {
		if matches(`подар|получател`) {
			ids = append(ids, "gift_recipient")
		} else {
			ids = append(ids, "trial")
		}
	}
	if (matches(`ссылк|qr`) || containsWord(text, "url") || containsWord(text, "урл")) && matches(`подпис|vpn|впн`) {
		ids = append(ids, "subscription_link")
	}
	giving := containsWord(text, "дам") || containsWord(text, "дать") || containsWord(text, "скину") || matches(`переда[тм]|отправ[ил]`)
	recipient := matches(`(?:^|[^\p{L}\p{N}_])(?:друг|подруг|семь|мам|пап|муж|жен|человек)`)
	referralLink := matches(`(?:реферал|партн[её]р)[\p{L}]*\s+ссылк|ссылк[\p{L}]*\s+(?:реферал|партн[её]р)`) ||
		matches(`реферал|партн[её]р`) && !matches(`подпис|совместн|вместе.*польз|польз.*вместе`)
	foreignAccess := matches(`(?:чуж[\p{L}]*|не\s+сво[\p{L}]*)\s+(?:аккаунт|ссылк|ключ|подписк)|без\s+(?:(?:его|е[её]|их)\s+)?(?:соглас|разреш)`)
	connectRecipient := matches(`подключ(?:ить|ать|у|им|аю).*\s(?:друг|подруг|семь|мам|пап|муж|жен|человек)`)
	permissionUse := recipient && matches(`польз`) && matches(`можно|нельзя|разреш|запрещ|может\s+ли`)
	if !referralLink && !foreignAccess && matches(`подпис|ссылк|vpn|впн|ключ`) && (matches(`подел|делиться|совместн|вместе.*польз|польз.*вместе`) ||
		giving && (matches(`можно`) || recipient) || connectRecipient || permissionUse) {
		ids = append(ids, "subscription_sharing")
	}
	if matches(`установить|скачать|подключить|установк`) && matches(`vpn|впн|happ|incy|устройств|телефон|планшет|ноутбук|компьютер|linux|линукс`) {
		ids = append(ids, "device_setup")
	}
	if matches(`n[/\\]a|не пингу|нет пинга|серверы.*не отвечают`) {
		ids = append(ids, "na_diagnosis", "na_apple", "na_windows_android")
	}
	if matches(`где.*кноп|найти.*кноп|какая.*кноп`) {
		ids = append(ids, "app_buttons")
	}
	return ids
}
func FormatFAQContext(results []domain.FAQResult) string {
	prefixes := make([]string, len(results))
	counts := map[string]int{}
	for i, r := range results {
		p := strings.Split(r.Answer, "\n\n")
		if len(p) > 2 {
			prefix := strings.Join(p[:2], "\n\n")
			if len([]rune(prefix)) >= 240 {
				prefixes[i] = prefix
				counts[prefix]++
			}
		}
	}
	numbers := map[string]int{}
	order := []string{}
	for _, prefix := range prefixes {
		if prefix != "" && counts[prefix] > 1 && numbers[prefix] == 0 {
			order = append(order, prefix)
			numbers[prefix] = len(order)
		}
	}
	var b strings.Builder
	b.WriteString("Кандидаты FAQ — справочные данные; применяй только подходящие условия и шаги.\n")
	for _, prefix := range order {
		fmt.Fprintf(&b, "Общие первые шаги %d (для статей со ссылкой на этот блок):\n%s\n\n", numbers[prefix], prefix)
	}
	for i, r := range results {
		answer := r.Answer
		if number := numbers[prefixes[i]]; number > 0 {
			answer = fmt.Sprintf("Сначала общие шаги %d.\n\n%s", number, answer[len(prefixes[i])+2:])
		}
		fmt.Fprintf(&b, "Вопрос: %s\nИнструкция: %s\n\n", r.Question, answer)
	}
	return b.String()
}

func containsWord(text, word string) bool {
	from := 0
	for {
		i := strings.Index(text[from:], word)
		if i < 0 {
			return false
		}
		i += from
		left, right := []rune(text[:i]), []rune(text[i+len(word):])
		isWord := func(r rune) bool { return unicode.IsLetter(r) || unicode.IsNumber(r) || r == '_' }
		if (len(left) == 0 || !isWord(left[len(left)-1])) && (len(right) == 0 || !isWord(right[0])) {
			return true
		}
		from = i + len(word)
		if from >= len(text) {
			return false
		}
	}
}
