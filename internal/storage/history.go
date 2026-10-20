package storage

import (
	"context"
	"strings"
	"supportbot/internal/diagnostics"
	"supportbot/internal/domain"
	"sync"
	"time"
	"unicode"
)

type History struct {
	store    *Store
	max      int
	ttl      time.Duration
	mu       sync.Mutex
	messages map[int64][]domain.Message
	activity map[int64]time.Time
	rejected map[int64]map[string]bool
	used     map[int64]map[string]bool
}

func NewHistory(s *Store, maxMessages, ttlDays int) *History {
	if maxMessages < 1 {
		maxMessages = 20
	}
	return &History{s, maxMessages, time.Duration(ttlDays) * 24 * time.Hour, sync.Mutex{}, map[int64][]domain.Message{}, map[int64]time.Time{}, map[int64]map[string]bool{}, map[int64]map[string]bool{}}
}
func (h *History) GetHistory(ctx context.Context, id int64) ([]domain.Message, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, ok := h.messages[id]; !ok {
		out := []domain.Message{}
		if h.store != nil {
			rows, err := h.store.Pool.Query(ctx, "SELECT role,content FROM chat_messages WHERE telegram_id=$1 ORDER BY created_at DESC,id DESC LIMIT $2", id, h.max)
			if err == nil {
				for rows.Next() {
					var m domain.Message
					if rows.Scan(&m.Role, &m.Content) != nil {
						break
					}
					out = append(out, m)
				}
				rows.Close()
			} else {
				diagnostics.Error(ctx, "Chat history database load failed", err)
			}
			for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
				out[i], out[j] = out[j], out[i]
			}
		}
		h.messages[id] = out
	}
	h.activity[id] = time.Now()
	diagnostics.Payload(ctx, "Chat history loaded", h.messages[id], "telegram_id", id)
	return append([]domain.Message{}, h.messages[id]...), nil
}
func (h *History) append(ctx context.Context, id int64, role, text string) error {
	text = strings.TrimSpace(text)
	if text == "" {
		return nil
	}
	if _, e := h.GetHistory(ctx, id); e != nil {
		return e
	}
	h.mu.Lock()
	h.messages[id] = append(h.messages[id], domain.Message{Role: role, Content: text})
	if len(h.messages[id]) > h.max {
		h.messages[id] = h.messages[id][len(h.messages[id])-h.max:]
	}
	h.activity[id] = time.Now()
	h.mu.Unlock()
	if h.store != nil {
		_, e := h.store.Pool.Exec(ctx, "INSERT INTO chat_messages(telegram_id,role,content,created_at) VALUES($1,$2,$3,now())", id, role, text)
		if e != nil {
			diagnostics.Error(ctx, "Chat history database write failed", e)
		}
	}
	return nil
}
func (h *History) AddUserMessage(ctx context.Context, id int64, text string) error {
	return h.append(ctx, id, "user", text)
}
func (h *History) AddAssistantMessage(ctx context.Context, id int64, text string) error {
	return h.append(ctx, id, "assistant", text)
}
func (h *History) GetLastUserMessage(id int64) string {
	h.mu.Lock()
	defer h.mu.Unlock()
	for i := len(h.messages[id]) - 1; i >= 0; i-- {
		if h.messages[id][i].Role == "user" {
			return h.messages[id][i].Content
		}
	}
	return ""
}
func (h *History) GetRejectedFAQQuestions(id int64) map[string]bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := map[string]bool{}
	for k, v := range h.rejected[id] {
		out[k] = v
	}
	return out
}
func (h *History) RecordFAQContext(id int64, f domain.FAQContext, used []string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.used[id] = map[string]bool{}
	questions := f.Questions()
	for _, q := range used {
		if questions[q] {
			h.used[id][q] = true
		}
	}
}
func (h *History) RejectLastFAQ(id int64) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.rejected[id] == nil {
		h.rejected[id] = map[string]bool{}
	}
	for q := range h.used[id] {
		h.rejected[id][q] = true
	}
}
func (h *History) ClearRejectedFAQsIfNewTopic(id int64, text string) {
	if strings.TrimSpace(text) == "" || IsRejection(text) {
		return
	}
	h.mu.Lock()
	delete(h.rejected, id)
	h.mu.Unlock()
}
func (h *History) Clear(ctx context.Context, id int64) error {
	h.mu.Lock()
	delete(h.messages, id)
	delete(h.activity, id)
	delete(h.rejected, id)
	delete(h.used, id)
	h.mu.Unlock()
	if h.store != nil {
		_, e := h.store.Pool.Exec(ctx, "DELETE FROM chat_messages WHERE telegram_id=$1", id)
		if e != nil {
			diagnostics.Error(ctx, "Chat history database write failed", e)
		}
	}
	return nil
}
func (h *History) EvictStaleEntries(ctx context.Context) error {
	cutoff := time.Now().Add(-h.ttl)
	h.mu.Lock()
	for id, at := range h.activity {
		if at.Before(cutoff) {
			delete(h.messages, id)
			delete(h.activity, id)
			delete(h.rejected, id)
			delete(h.used, id)
		}
	}
	h.mu.Unlock()
	if h.store != nil {
		_, e := h.store.Pool.Exec(ctx, "DELETE FROM chat_messages WHERE created_at<$1", cutoff)
		if e != nil {
			diagnostics.Error(ctx, "Chat history database write failed", e)
		}
	}
	return nil
}

// IsRejection uses Unicode word boundaries, matching Python's Russian phrase detector.
func IsRejection(text string) bool {
	s := strings.ReplaceAll(strings.ToLower(text), "ё", "е")
	if strings.TrimRight(s, ".!?") == "другое" {
		return true
	}
	for _, p := range []string{"не то", "не та", "не это", "не подходит", "не помог", "не помогло", "другой вариант", "другая инструкция", "нет, я про другое", "все равно не", "все еще не", "по-прежнему", "по прежнему", "ничего не изменилось", "ничего не поменялось", "ничего не помогает", "то же самое", "тоже самое", "опять не работает", "снова не работает", "также не работает", "так же не работает", "не заработало", "без изменений"} {
		from := 0
		for {
			idx := strings.Index(s[from:], p)
			if idx < 0 {
				break
			}
			idx += from
			left := []rune(s[:idx])
			right := []rune(s[idx+len(p):])
			word := func(r rune) bool { return unicode.IsLetter(r) || unicode.IsNumber(r) || r == '_' }
			if (len(left) == 0 || !word(left[len(left)-1])) && (len(right) == 0 || !word(right[0])) {
				return true
			}
			from = idx + len(p)
			if from >= len(s) {
				break
			}
		}
	}
	return false
}
