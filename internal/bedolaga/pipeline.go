package bedolaga

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"supportbot/internal/bot"
	"supportbot/internal/diagnostics"
	"supportbot/internal/domain"
	"supportbot/internal/telegram"
	"sync"
	"time"
)

type TicketAPI interface {
	GetTicket(context.Context, int64) *Ticket
	List(context.Context, int) []int64
	Reply(context.Context, int64, string) *PostedReply
	ReplyPhoto(context.Context, int64, string, string, string) *PostedReply
	Priority(context.Context, int64, string) bool
	Resolve(context.Context, int64) Lookup
	DescribeMedia(context.Context, int64, int64, string) *telegram.Media
	Image(context.Context, *telegram.Media) (string, string)
}
type ticketBackoff struct {
	Failures int
	At       time.Time
}
type Answerer struct {
	Client        TicketAPI
	LLM           domain.LLM
	Store         domain.Store
	Rate          *bot.RateLimiter
	Notifier      *bot.Notifier
	Forwarder     *bot.Forwarder
	Gaps          domain.Gaps
	State         *bot.ConversationState
	Now           func() time.Time
	locks         bot.KeyedLock
	slots         chan struct{}
	mu            sync.Mutex
	active, rerun map[int64]bool
	suppressed    map[int64]int64
	backoff       map[int64]ticketBackoff
	wg            sync.WaitGroup
	ctx           context.Context
	cancel        context.CancelFunc
	closed        bool
}

func NewAnswerer(ctx context.Context, c TicketAPI, llm domain.LLM, store domain.Store, rate *bot.RateLimiter, notifier *bot.Notifier, forward *bot.Forwarder, gaps domain.Gaps, state *bot.ConversationState, max int) *Answerer {
	if max < 1 {
		max = 1
	}
	work, cancel := context.WithCancel(ctx)
	return &Answerer{Client: c, LLM: llm, Store: store, Rate: rate, Notifier: notifier, Forwarder: forward, Gaps: gaps, State: state, Now: time.Now, slots: make(chan struct{}, max), active: map[int64]bool{}, rerun: map[int64]bool{}, suppressed: map[int64]int64{}, backoff: map[int64]ticketBackoff{}, ctx: work, cancel: cancel}
}
func (a *Answerer) Schedule(id int64) {
	a.mu.Lock()
	if a.closed {
		a.mu.Unlock()
		return
	}
	if a.active[id] {
		a.rerun[id] = true
		a.mu.Unlock()
		return
	}
	a.active[id] = true
	a.wg.Add(1)
	a.mu.Unlock()
	go func() {
		defer a.wg.Done()
		for {
			a.mu.Lock()
			delete(a.rerun, id)
			a.mu.Unlock()
			a.Handle(a.ctx, id)
			a.mu.Lock()
			if !a.rerun[id] || a.ctx.Err() != nil {
				delete(a.active, id)
				delete(a.rerun, id)
				a.mu.Unlock()
				return
			}
			a.mu.Unlock()
		}
	}()
}
func (a *Answerer) markRerun(id int64) { a.mu.Lock(); a.rerun[id] = true; a.mu.Unlock() }
func (a *Answerer) Drain(ctx context.Context) {
	a.mu.Lock()
	a.closed = true
	a.mu.Unlock()
	done := make(chan struct{})
	go func() { a.wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-ctx.Done():
		a.cancel()
	}
}
func (a *Answerer) Handle(ctx context.Context, id int64) {
	if diagnostics.CorrelationID(ctx) == "" {
		ctx = diagnostics.WithCorrelation(ctx, diagnostics.NewID())
	}
	ctx = diagnostics.WithOperation(ctx, "bedolaga", "ticket_turn")
	diagnostics.Payload(ctx, "Bedolaga ticket turn metadata", map[string]any{"ticket_id": id})
	select {
	case a.slots <- struct{}{}:
	case <-ctx.Done():
		return
	}
	defer func() { <-a.slots }()
	unlock, e := a.locks.Lock(ctx, id)
	if e != nil {
		return
	}
	defer unlock()
	if e = a.answer(ctx, id); e != nil && ctx.Err() == nil {
		diagnostics.Error(ctx, "Bedolaga ticket processing failed", e)
		if a.Notifier != nil {
			a.Notifier.Notify(ctx, bot.Message("bedolaga.error.context", id), 0, e)
		}
	}
}
func (a *Answerer) backingOff(id int64) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	entry, ok := a.backoff[id]
	return ok && a.Now().Before(entry.At)
}
func (a *Answerer) clear(id int64) {
	a.mu.Lock()
	delete(a.backoff, id)
	delete(a.suppressed, id)
	a.mu.Unlock()
}
func (a *Answerer) replyFailed(ctx context.Context, id, user int64) {
	a.mu.Lock()
	entry := a.backoff[id]
	entry.Failures++
	if entry.Failures > 16 {
		entry.Failures = 16
	}
	delay := time.Minute * time.Duration(1<<(entry.Failures-1))
	if delay > 30*time.Minute {
		delay = 30 * time.Minute
	}
	entry.At = a.Now().Add(delay)
	a.backoff[id] = entry
	a.mu.Unlock()
	if a.Notifier != nil {
		a.Notifier.Notify(ctx, bot.Message("bedolaga.reply.failed", id), user, nil)
	}
}
func Unanswered(t Ticket, p domain.TicketProgress) *TicketMessage {
	m := t.LastUser()
	if m == nil || p.LastAnsweredMessageID >= m.ID {
		return nil
	}
	if t.Awaits() || (t.Status == "answered" && p.LastBotReplyMessageID > m.ID) {
		return m
	}
	return nil
}
func (a *Answerer) human(ctx context.Context, t Ticket, p domain.TicketProgress, user int64) (bool, error) {
	latest := int64(0)
	if p.LastBotReplyMessageID > 0 {
		for _, m := range t.Messages {
			if m.Admin && m.ID > p.LastBotReplyMessageID && m.ID > latest {
				latest = m.ID
			}
		}
	}
	if latest == 0 {
		return a.State.OperatorActive(user), nil
	}
	if latest > p.LastHumanReplyMessageID {
		if e := a.Store.RecordHumanReply(ctx, t.ID, latest, a.Now().UTC()); e != nil {
			return false, e
		}
		a.State.RecordOperator(user)
		return true, nil
	}
	return a.State.OperatorActive(user) || p.HumanRecentlyActive(a.Now(), a.State.Suppression), nil
}
func (a *Answerer) mirror(ctx context.Context, t Ticket, u *telegram.User, text string, escalate bool) {
	if a.Forwarder != nil {
		a.Forwarder.Forward(ctx, u.ID, nil, u, text, escalate, 0, &t.ID)
	}
}
func (a *Answerer) pendingMedia(ctx context.Context, t Ticket, u *telegram.User, p domain.TicketProgress) (map[int64]*telegram.Media, error) {
	items := []TicketMessage{}
	for _, m := range t.Messages {
		if !m.Admin && m.HasMedia && m.ID > p.LastMirroredMediaMessageID {
			items = append(items, m)
		}
	}
	sort.Slice(items, func(i, j int) bool { return items[i].ID < items[j].ID })
	out := map[int64]*telegram.Media{}
	if len(items) > 0 {
		if e := a.Store.RecordPendingMedia(ctx, t.ID, items[len(items)-1].ID); e != nil {
			return nil, e
		}
	}
	for _, m := range items {
		media := a.Client.DescribeMedia(ctx, t.ID, m.ID, m.MediaType)
		out[m.ID] = media
		if a.Forwarder == nil || a.Forwarder.ForwardMedia(ctx, u, t.ID, media) == 0 {
			break
		}
		if e := a.Store.RecordMirroredMedia(ctx, t.ID, m.ID); e != nil {
			return nil, e
		}
	}
	return out, nil
}
func (a *Answerer) reconcile(ctx context.Context, id, answered int64) {
	t := a.Client.GetTicket(ctx, id)
	if t != nil {
		u := t.LastUser()
		if u != nil && u.ID > answered {
			a.markRerun(id)
		}
	}
}
func (a *Answerer) answer(ctx context.Context, id int64) error {
	ticket := a.Client.GetTicket(ctx, id)
	if ticket == nil {
		a.clear(id)
		return nil
	}
	t := *ticket
	p, e := a.Store.TicketProgress(ctx, id)
	if e != nil {
		return e
	}
	last := Unanswered(t, p)
	hasMedia := false
	for _, m := range t.Messages {
		if !m.Admin && m.HasMedia && m.ID > p.LastMirroredMediaMessageID {
			hasMedia = true
		}
	}
	if last == nil && !hasMedia {
		a.clear(id)
		return nil
	}
	if last != nil && a.backingOff(id) && !hasMedia {
		return nil
	}
	lookup := a.Client.Resolve(ctx, t.UserID)
	if !lookup.Known {
		return nil
	}
	u := lookup.User
	u.ID = -t.UserID
	if lookup.TelegramID != nil && *lookup.TelegramID != 0 {
		u.ID = *lookup.TelegramID
	}
	mediaMap, e := a.pendingMedia(ctx, t, &u, p)
	if e != nil {
		return e
	}
	if last == nil {
		a.clear(id)
		return nil
	}
	if a.backingOff(id) {
		return nil
	}
	question := t.Question(last)
	human, e := a.human(ctx, t, p, u.ID)
	if e != nil {
		return e
	}
	if human {
		a.mu.Lock()
		seen := a.suppressed[id] == last.ID
		a.suppressed[id] = last.ID
		a.mu.Unlock()
		if !seen {
			a.mirror(ctx, t, &u, bot.Message("bedolaga.suppressed", t.ID, t.Title, question), true)
		}
		return nil
	}
	if !a.Rate.Acquire(u.ID) {
		return nil
	}
	media, resolved := mediaMap[last.ID]
	if !resolved && last.HasMedia {
		media = a.Client.DescribeMedia(ctx, t.ID, last.ID, last.MediaType)
	}
	image, mime := "", ""
	if media != nil && media.Type == "photo" && a.LLM.SupportsImages() {
		image, mime = a.Client.Image(ctx, media)
	}
	if strings.TrimSpace(question) == "" && image == "" {
		posted := a.Client.Reply(ctx, t.ID, bot.Message("bedolaga.nothing.to.answer"))
		if posted == nil {
			a.replyFailed(ctx, t.ID, u.ID)
			return nil
		}
		if e = a.Store.RecordTicketReply(ctx, t.ID, posted.MessageID, &last.ID); e != nil {
			return e
		}
		a.clear(t.ID)
		a.reconcile(ctx, t.ID, last.ID)
		a.mirror(ctx, t, &u, bot.Message("bedolaga.nothing.mirror", t.ID, t.Title), true)
		return nil
	}
	var reply domain.Reply
	if image != "" {
		prompt := strings.TrimSpace(question)
		if prompt == "" {
			prompt = bot.Message("bot.photo.default.prompt")
		}
		reply, e = a.LLM.ChatWithImage(ctx, prompt, u.ID, image, mime)
	} else {
		reply, e = a.LLM.Chat(ctx, question, u.ID)
	}
	if e != nil {
		return e
	}
	answer := bot.StripMarker(reply.Text)
	if answer == "" {
		answer = bot.Message("bedolaga.llm.empty")
	}
	escalate := bot.Escalate(question, reply.Text)
	note := ""
	if escalate {
		note = bot.Message("bedolaga.escalation.note")
	}
	answer = bot.Truncate(answer, MaxReplyLength-len([]rune(note)))
	fresh := a.Client.GetTicket(ctx, t.ID)
	if fresh == nil {
		return nil
	}
	if fresh.Status != t.Status && (!OpenStatus(fresh.Status) || !OpenStatus(t.Status)) {
		a.clear(t.ID)
		return nil
	}
	snapshot := last.ID
	if m := t.Last(); m != nil {
		snapshot = m.ID
	}
	newUser := false
	for _, m := range fresh.Messages {
		if m.ID > snapshot {
			if m.Admin {
				a.mu.Lock()
				delete(a.backoff, t.ID)
				a.mu.Unlock()
				return nil
			}
			newUser = true
		}
	}
	if newUser {
		a.markRerun(t.ID)
		return nil
	}
	posted := a.Client.Reply(ctx, t.ID, answer+note)
	if posted == nil {
		a.replyFailed(ctx, t.ID, u.ID)
		return nil
	}
	if e = a.Store.RecordTicketReply(ctx, t.ID, posted.MessageID, &last.ID); e != nil {
		return e
	}
	a.clear(t.ID)
	a.State.RecordQuery(u.ID, question, reply.FAQContext)
	a.reconcile(ctx, t.ID, last.ID)
	if escalate {
		_ = a.Client.Priority(ctx, t.ID, "high")
	}
	a.mirror(ctx, t, &u, bot.Message("bedolaga.mirror", t.ID, t.Title, question, answer), escalate)
	if a.Gaps != nil && strings.TrimSpace(question) != "" {
		return a.Gaps.Evaluate(ctx, question, u.ID, reply.Text, reply.FAQContext)
	}
	return nil
}
func (a *Answerer) Sweep(ctx context.Context, limit int) {
	ids := a.Client.List(ctx, limit)
	pending, e := a.Store.PendingMediaTicketIDs(ctx, limit)
	if e == nil {
		ids = append(ids, pending...)
	}
	seen := map[int64]bool{}
	for _, id := range ids {
		if !seen[id] {
			seen[id] = true
			a.Schedule(id)
		}
	}
}

type Relay struct {
	Client TicketAPI
	Store  domain.Store
	State  *bot.ConversationState
}

func (r *Relay) record(ctx context.Context, ticket, user int64, posted *PostedReply) bool {
	if posted == nil {
		return false
	}
	if posted.MessageID != nil {
		if e := r.Store.RecordHumanReply(ctx, ticket, *posted.MessageID, time.Now().UTC()); e != nil {
			diagnostics.Error(ctx, "Accepted operator reply state recording failed", e)
		}
	}
	r.State.RecordOperator(user)
	return true
}
func (r *Relay) ReplyText(ctx context.Context, ticket, user int64, text string) bool {
	return r.record(ctx, ticket, user, r.Client.Reply(ctx, ticket, text))
}
func (r *Relay) ReplyPhoto(ctx context.Context, ticket, user int64, data, mime, caption string) bool {
	return r.record(ctx, ticket, user, r.Client.ReplyPhoto(ctx, ticket, caption, data, mime))
}

var _ = fmt.Sprint
