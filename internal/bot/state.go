package bot

import (
	"context"
	"supportbot/internal/domain"
	"sync"
	"time"
)

type LastQuery struct {
	Text string
	FAQ  domain.FAQContext
	At   time.Time
}
type ConversationState struct {
	mu               sync.Mutex
	Now              func() time.Time
	Suppression, TTL time.Duration
	queries          map[int64]LastQuery
	operators        map[int64]time.Time
	pictures         map[int64]map[string]bool
	pictureAt        map[int64]time.Time
}

func NewConversationState(suppression, ttl time.Duration) *ConversationState {
	return &ConversationState{Now: time.Now, Suppression: suppression, TTL: ttl, queries: map[int64]LastQuery{}, operators: map[int64]time.Time{}, pictures: map[int64]map[string]bool{}, pictureAt: map[int64]time.Time{}}
}
func (s *ConversationState) RecordQuery(id int64, text string, faq domain.FAQContext) {
	if text == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.queries[id] = LastQuery{text, faq, s.Now()}
}
func (s *ConversationState) LastQuery(id int64) *LastQuery {
	s.mu.Lock()
	defer s.mu.Unlock()
	q, ok := s.queries[id]
	if !ok {
		return nil
	}
	if s.Now().Sub(q.At) >= s.TTL {
		delete(s.queries, id)
		return nil
	}
	return &q
}
func (s *ConversationState) RecordOperator(id int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.operators[id] = s.Now()
}
func (s *ConversationState) OperatorActive(id int64) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	at, ok := s.operators[id]
	if ok && s.Now().Sub(at) < s.Suppression {
		return true
	}
	delete(s.operators, id)
	return false
}
func (s *ConversationState) PictureSeen(id int64, name string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.Now().Sub(s.pictureAt[id]) >= s.TTL {
		delete(s.pictures, id)
		delete(s.pictureAt, id)
	}
	return s.pictures[id][name]
}
func (s *ConversationState) RecordPicture(id int64, name string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.pictures[id] == nil {
		s.pictures[id] = map[string]bool{}
	}
	s.pictures[id][name] = true
	s.pictureAt[id] = s.Now()
}
func (s *ConversationState) Clear(id int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.queries, id)
	delete(s.operators, id)
	delete(s.pictures, id)
	delete(s.pictureAt, id)
}
func (s *ConversationState) Evict() {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.Now()
	for id, q := range s.queries {
		if now.Sub(q.At) >= s.TTL {
			delete(s.queries, id)
		}
	}
	for id, at := range s.operators {
		if now.Sub(at) >= s.Suppression {
			delete(s.operators, id)
		}
	}
	for id, at := range s.pictureAt {
		if now.Sub(at) >= s.TTL {
			delete(s.pictureAt, id)
			delete(s.pictures, id)
		}
	}
}

type RateLimiter struct {
	mu                  sync.Mutex
	Now                 func() time.Time
	Interval, Retention time.Duration
	last                map[int64]time.Time
}

func NewRateLimiter() *RateLimiter {
	return &RateLimiter{Now: time.Now, Interval: 3 * time.Second, Retention: time.Minute, last: map[int64]time.Time{}}
}
func (r *RateLimiter) Acquire(id int64) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.Now()
	at, ok := r.last[id]
	if !ok || now.Sub(at) >= r.Interval {
		r.last[id] = now
		return true
	}
	return false
}
func (r *RateLimiter) Evict() {
	r.mu.Lock()
	defer r.mu.Unlock()
	for id, at := range r.last {
		if r.Now().Sub(at) > r.Retention {
			delete(r.last, id)
		}
	}
}

type lockEntry struct {
	token chan struct{}
	refs  int
}
type KeyedLock struct {
	mu      sync.Mutex
	entries map[int64]*lockEntry
}

func (k *KeyedLock) Lock(ctx context.Context, id int64) (func(), error) {
	k.mu.Lock()
	if k.entries == nil {
		k.entries = map[int64]*lockEntry{}
	}
	e := k.entries[id]
	if e == nil {
		e = &lockEntry{token: make(chan struct{}, 1)}
		e.token <- struct{}{}
		k.entries[id] = e
	}
	e.refs++
	k.mu.Unlock()
	releaseRef := func() {
		k.mu.Lock()
		e.refs--
		if e.refs == 0 {
			delete(k.entries, id)
		}
		k.mu.Unlock()
	}
	select {
	case <-ctx.Done():
		releaseRef()
		return nil, ctx.Err()
	case <-e.token:
		return func() { e.token <- struct{}{}; releaseRef() }, nil
	}
}
