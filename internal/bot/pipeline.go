package bot

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"supportbot/internal/diagnostics"
	"supportbot/internal/domain"
	"supportbot/internal/telegram"
	"sync"
	"time"
	"unicode"
)

func StripMarker(s string) string { return strings.TrimSpace(strings.ReplaceAll(s, "[ESCALATE]", "")) }
func RequestsHuman(s string) bool {
	words := strings.FieldsFunc(strings.ToLower(s), func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsDigit(r) && r != '_' })
	for _, w := range words {
		if strings.HasPrefix(w, "оператор") || strings.HasPrefix(w, "человек") || strings.HasPrefix(w, "человеч") || w == "живой" || w == "живого" || w == "живому" || w == "живым" || w == "живом" {
			return true
		}
	}
	return false
}
func Escalate(query, reply string) bool {
	return strings.Contains(reply, "[ESCALATE]") || RequestsHuman(query)
}

type Pipeline struct {
	LLM       domain.LLM
	Sender    *telegram.Sender
	Forwarder *Forwarder
	Rate      *RateLimiter
	Gaps      domain.Gaps
	State     *ConversationState
	locks     KeyedLock
}

func StartTyping(ctx context.Context, api telegram.API, chat int64) func() {
	work, cancel := context.WithCancel(ctx)
	go func() {
		for {
			_ = api.Call(work, "sendChatAction", map[string]any{"chat_id": chat, "action": "typing"}, nil)
			select {
			case <-work.Done():
				return
			case <-time.After(4 * time.Second):
			}
		}
	}()
	return cancel
}
func (p *Pipeline) Handle(ctx context.Context, b Batch) {
	ctx = operationContext(ctx, "user_turn")
	diagnostics.Payload(ctx, "User pipeline batch metadata", map[string]any{"chat_id": b.Last.Chat.ID, "message_ids": b.IDs, "text": b.Text, "has_image": b.Image != "", "has_pdf": b.PDF != ""})
	chat := b.Last.Chat.ID
	user := chat
	if b.User != nil && b.User.ID != 0 {
		user = b.User.ID
	}
	if chat == 0 || user == 0 {
		return
	}
	unlock, e := p.locks.Lock(ctx, user)
	if e != nil {
		return
	}
	defer unlock()
	if p.State.OperatorActive(user) {
		p.Forwarder.Forward(ctx, chat, b.IDs, b.User, Message("support.ai.suppressed"), false, 0, nil)
		return
	}
	if !p.Rate.Acquire(user) {
		p.Sender.Send(ctx, chat, 0, 0, Message("bot.ratelimit.wait"))
		p.Forwarder.Forward(ctx, chat, b.IDs, b.User, Message("support.ratelimited"), true, 0, nil)
		return
	}
	stop := StartTyping(ctx, p.Sender.API, chat)
	defer stop()
	var reply domain.Reply
	if b.PDF != "" {
		reply, e = p.LLM.ChatWithPDF(ctx, b.Text, user, b.PDF)
	} else if b.Image != "" {
		reply, e = p.LLM.ChatWithImage(ctx, b.Text, user, b.Image, b.MIME)
	} else {
		reply, e = p.LLM.Chat(ctx, b.Text, user)
	}
	if e != nil {
		diagnostics.Error(ctx, "User model turn failed", e)
		visible := Message("bot.llm.error")
		var friendly interface{ UserMessage() string }
		if errors.As(e, &friendly) {
			visible = friendly.UserMessage()
		}
		p.Sender.Send(ctx, chat, 0, 0, visible)
		p.Forwarder.Error(ctx, b.User, b.Text, visible, "Bot: "+Truncate(e.Error(), 3000))
		return
	}
	if p.State.OperatorActive(user) {
		p.Forwarder.Forward(ctx, chat, b.IDs, b.User, Message("support.ai.suppressed"), false, 0, nil)
		return
	}
	p.State.RecordQuery(user, b.Text, reply.FAQContext)
	response := StripMarker(reply.Text)
	if response == "" {
		response = Message("bot.llm.empty")
	}
	p.Sender.Send(ctx, chat, 0, 0, response)
	picture := SendIllustration(ctx, p.Sender, p.State, chat, user, reply.FAQContext, response)
	p.Forwarder.Forward(ctx, chat, b.IDs, b.User, response, Escalate(b.Text, reply.Text), picture, nil)
	if p.Gaps != nil && strings.TrimSpace(b.UserText) != "" {
		_ = p.Gaps.Evaluate(ctx, b.UserText, user, reply.Text, reply.FAQContext)
	}
}

type alertEntry struct {
	At    time.Time
	Count int
}
type Notifier struct {
	API     telegram.API
	Group   int64
	Now     func() time.Time
	mu      sync.Mutex
	entries map[string]alertEntry
}

func (n *Notifier) Notify(ctx context.Context, label string, user int64, err error) {
	n.mu.Lock()
	if n.Now == nil {
		n.Now = time.Now
	}
	if n.entries == nil {
		n.entries = map[string]alertEntry{}
	}
	now := n.Now()
	for key, value := range n.entries {
		if now.Sub(value.At) > 20*time.Minute {
			delete(n.entries, key)
		}
	}
	entry, ok := n.entries[label]
	if ok && now.Sub(entry.At) < 10*time.Minute {
		entry.Count++
		n.entries[label] = entry
		n.mu.Unlock()
		return
	}
	n.entries[label] = alertEntry{At: now}
	n.mu.Unlock()
	lines := []string{Message("admin.error.prefix"), label}
	if user != 0 {
		lines = append(lines, fmt.Sprintf("User: %d", user))
	}
	if entry.Count > 0 {
		lines = append(lines, Message("admin.error.suppressed", entry.Count))
	}
	details := "null"
	if err != nil {
		details = Truncate(err.Error(), 2000)
	}
	lines = append(lines, "", details)
	e := n.API.Call(ctx, "sendMessage", map[string]any{"chat_id": n.Group, "text": strings.Join(lines, "\n"), "disable_notification": true}, nil)
	if e != nil {
		diagnostics.Error(ctx, "Admin notification delivery failed", e)
	}
}

func operationContext(ctx context.Context, operation string) context.Context {
	if diagnostics.CorrelationID(ctx) == "" {
		ctx = diagnostics.WithCorrelation(ctx, diagnostics.NewID())
	}
	return diagnostics.WithOperation(ctx, "bot", operation)
}
func observeError(ctx context.Context, operation string, err error) {
	if err != nil {
		diagnostics.Error(ctx, operation, err)
	}
}
