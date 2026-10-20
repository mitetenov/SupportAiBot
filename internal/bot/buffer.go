package bot

import (
	"context"
	"strings"
	"supportbot/internal/telegram"
	"sync"
	"time"
)

type BufferedMessage struct {
	Message                          telegram.Message
	Text, UserText, Image, MIME, PDF string
}
type Batch struct {
	Last                             telegram.Message
	User                             *telegram.User
	Text, UserText, Image, MIME, PDF string
	IDs                              []int64
}

func MakeBatch(items []BufferedMessage) Batch {
	last := items[len(items)-1].Message
	b := Batch{Last: last, User: last.From}
	var texts, authored []string
	for _, item := range items {
		b.IDs = append(b.IDs, item.Message.ID)
		if strings.TrimSpace(item.Text) != "" {
			texts = append(texts, item.Text)
		}
		if strings.TrimSpace(item.UserText) != "" {
			authored = append(authored, item.UserText)
		}
		if b.Image == "" && item.Image != "" {
			b.Image = item.Image
			b.MIME = item.MIME
		}
		if b.PDF == "" {
			b.PDF = item.PDF
		}
	}
	b.Text = strings.Join(texts, "\n")
	b.UserText = strings.Join(authored, "\n")
	return b
}

type pendingBatch struct {
	items      []BufferedMessage
	timer      *time.Timer
	generation uint64
}
type Buffer struct {
	mu      sync.Mutex
	Window  time.Duration
	Max     int
	pending map[int64]*pendingBatch
	wg      sync.WaitGroup
	ctx     context.Context
	cancel  context.CancelFunc
	Sink    func(context.Context, Batch)
	closed  bool
}

func NewBuffer(ctx context.Context, window time.Duration, max int, sink func(context.Context, Batch)) *Buffer {
	work, cancel := context.WithCancel(ctx)
	return &Buffer{Window: window, Max: max, pending: map[int64]*pendingBatch{}, ctx: work, cancel: cancel, Sink: sink}
}
func (b *Buffer) dispatchLocked(items []BufferedMessage) {
	batch := MakeBatch(items)
	b.wg.Add(1)
	go func() { defer b.wg.Done(); b.Sink(b.ctx, batch) }()
}
func (b *Buffer) flushLocked(id int64) {
	p := b.pending[id]
	if p == nil {
		return
	}
	if p.timer != nil {
		p.timer.Stop()
	}
	delete(b.pending, id)
	if len(p.items) > 0 {
		b.dispatchLocked(p.items)
	}
}
func (b *Buffer) Submit(id int64, m BufferedMessage) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return
	}
	p := b.pending[id]
	if p != nil && (m.Image != "" || m.PDF != "") {
		for _, old := range p.items {
			if old.Image != "" || old.PDF != "" {
				b.flushLocked(id)
				p = nil
				break
			}
		}
	}
	if p == nil {
		p = &pendingBatch{}
		b.pending[id] = p
	}
	if p.timer != nil {
		p.timer.Stop()
	}
	p.items = append(p.items, m)
	p.generation++
	if len(p.items) >= b.Max {
		b.flushLocked(id)
		return
	}
	generation := p.generation
	p.timer = time.AfterFunc(b.Window, func() {
		b.mu.Lock()
		defer b.mu.Unlock()
		current := b.pending[id]
		if current == p && current.generation == generation {
			b.flushLocked(id)
		}
	})
}
func (b *Buffer) Drain(ctx context.Context) {
	b.mu.Lock()
	b.closed = true
	for id := range b.pending {
		b.flushLocked(id)
	}
	b.mu.Unlock()
	done := make(chan struct{})
	go func() { b.wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-ctx.Done():
		b.cancel()
	}
}
func (b *Buffer) Shutdown() {
	b.mu.Lock()
	b.closed = true
	for id, p := range b.pending {
		if p.timer != nil {
			p.timer.Stop()
		}
		delete(b.pending, id)
	}
	b.mu.Unlock()
	b.cancel()
}

func (b *Buffer) Discard(id int64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if pending := b.pending[id]; pending != nil {
		if pending.timer != nil {
			pending.timer.Stop()
		}
		delete(b.pending, id)
	}
}
