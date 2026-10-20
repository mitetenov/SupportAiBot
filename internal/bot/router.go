package bot

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"supportbot/internal/diagnostics"
	"supportbot/internal/domain"
	"supportbot/internal/telegram"
)

type TicketRelay interface {
	ReplyText(context.Context, int64, int64, string) bool
	ReplyPhoto(context.Context, int64, int64, string, string, string) bool
}
type Router struct {
	Sender    *telegram.Sender
	LLM       domain.LLM
	Store     domain.Store
	History   domain.History
	Gaps      domain.Gaps
	State     *ConversationState
	Forwarder *Forwarder
	Buffer    *Buffer
	Pipeline  *Pipeline
	Group     int64
	Admins    map[int64]bool
	Relay     TicketRelay
}

func (r *Router) Handle(ctx context.Context, u telegram.Update) {
	ctx = operationContext(ctx, "telegram_update")
	diagnostics.Payload(ctx, "Telegram update", u)
	if u.Callback != nil {
		_ = r.Sender.API.Call(ctx, "answerCallbackQuery", map[string]any{"callback_query_id": u.Callback.ID}, nil)
		return
	}
	if u.Reaction != nil {
		r.reaction(ctx, *u.Reaction)
		return
	}
	if u.Message == nil {
		return
	}
	m := *u.Message
	if m.From == nil || m.From.IsBot {
		return
	}
	if m.Chat.ID == r.Group {
		r.operator(ctx, m)
		return
	}
	observeError(ctx, "Telegram user profile recording failed", r.Store.UpsertUser(ctx, domain.User{TelegramID: m.From.ID, Username: m.From.Username, FirstName: m.From.FirstName, LastName: m.From.LastName}))
	observeError(ctx, "Telegram active ticket clear failed", r.Store.SetActiveTicket(ctx, m.From.ID, nil))
	text := strings.TrimSpace(m.Text)
	if strings.HasPrefix(text, "/") {
		r.command(ctx, m, text)
		return
	}
	if text != "" {
		r.Buffer.Submit(m.From.ID, BufferedMessage{Message: m, Text: text, UserText: text})
		return
	}
	if len(m.Photo) > 0 {
		if !r.LLM.SupportsImages() {
			r.mediaUnsupported(ctx, m, "bot.photo.notsupported")
			return
		}
		data, mime, key := r.downloadPhoto(ctx, m.Photo)
		if key != "" {
			r.Sender.Send(ctx, m.Chat.ID, 0, 0, Message(key))
			return
		}
		caption := strings.TrimSpace(m.Caption)
		prompt := caption
		if prompt == "" {
			prompt = Message("bot.photo.default.prompt")
		}
		r.Buffer.Submit(m.From.ID, BufferedMessage{Message: m, Text: prompt, UserText: caption, Image: data, MIME: mime})
		return
	}
	if m.Document != nil && (m.Document.MIME == "application/pdf" || strings.HasSuffix(strings.ToLower(m.Document.Name), ".pdf")) {
		if !r.LLM.SupportsPDF() {
			r.mediaUnsupported(ctx, m, "bot.pdf.notsupported")
			return
		}
		data, key := r.downloadPDF(ctx, *m.Document)
		if key != "" {
			r.Sender.Send(ctx, m.Chat.ID, 0, 0, Message(key))
			return
		}
		caption := strings.TrimSpace(m.Caption)
		prompt := caption
		if prompt == "" {
			prompt = Message("bot.pdf.default.prompt")
		}
		r.Buffer.Submit(m.From.ID, BufferedMessage{Message: m, Text: prompt, UserText: caption, PDF: data})
		return
	}
	r.mediaUnsupported(ctx, m, "bot.media.unsupported")
}
func (r *Router) mediaUnsupported(ctx context.Context, m telegram.Message, key string) {
	r.Sender.Send(ctx, m.Chat.ID, 0, 0, Message(key))
	r.Forwarder.Forward(ctx, m.Chat.ID, []int64{m.ID}, m.From, Message("support.media.received"), false, 0, nil)
}
func (r *Router) downloadPhoto(ctx context.Context, photos []telegram.File) (string, string, string) {
	if len(photos) == 0 || photos[len(photos)-1].ID == "" {
		return "", "", "bot.photo.upload.error"
	}
	var f telegram.File
	if r.Sender.API.Call(ctx, "getFile", map[string]any{"file_id": photos[len(photos)-1].ID}, &f) != nil || f.Path == "" {
		return "", "", "bot.photo.upload.error"
	}
	b, e := r.Sender.API.Download(ctx, f.Path, 50<<20)
	if e != nil {
		return "", "", "bot.photo.error"
	}
	if len(b) == 0 {
		return "", "", "bot.photo.download.error"
	}
	mime := "image/jpeg"
	switch strings.ToLower(filepath.Ext(f.Path)) {
	case ".png":
		mime = "image/png"
	case ".webp":
		mime = "image/webp"
	case ".gif":
		mime = "image/gif"
	}
	return base64.StdEncoding.EncodeToString(b), mime, ""
}
func (r *Router) downloadPDF(ctx context.Context, d telegram.File) (string, string) {
	if d.MIME != "application/pdf" && !strings.HasSuffix(strings.ToLower(d.Name), ".pdf") {
		return "", "bot.pdf.invalid"
	}
	if d.ID == "" {
		return "", "bot.pdf.error"
	}
	if d.Size > 20_000_000 {
		return "", "bot.pdf.too_large"
	}
	var f telegram.File
	if r.Sender.API.Call(ctx, "getFile", map[string]any{"file_id": d.ID}, &f) != nil || f.Path == "" {
		return "", "bot.pdf.error"
	}
	if f.Size > 20_000_000 {
		return "", "bot.pdf.too_large"
	}
	b, e := r.Sender.API.Download(ctx, f.Path, 20_000_000)
	if errors.Is(e, telegram.ErrTooLarge) {
		return "", "bot.pdf.too_large"
	}
	if e != nil || len(b) == 0 {
		return "", "bot.pdf.error"
	}
	if !strings.HasPrefix(string(b), "%PDF-") {
		return "", "bot.pdf.invalid"
	}
	return base64.StdEncoding.EncodeToString(b), ""
}
func (r *Router) reaction(ctx context.Context, v telegram.Reaction) {
	var m *domain.MessageMapping
	var e error
	if v.Chat.ID == r.Group {
		m, e = r.Store.MappingByTopicMessage(ctx, 0, v.MessageID)
	} else {
		m, e = r.Store.MappingByUserMessage(ctx, v.Chat.ID, v.MessageID)
	}
	if e != nil || m == nil {
		return
	}
	if v.Chat.ID == r.Group {
		r.Sender.Reaction(ctx, m.UserChatID, m.UserMessageID, v.New)
	} else {
		r.Sender.Reaction(ctx, r.Group, m.TopicMessageID, v.New)
	}
}

var askPattern = regexp.MustCompile(`(?i)^/ask(?:@\w+)?(?:\s+|$)`)

func ParseAsk(s string) (string, bool) {
	s = strings.TrimSpace(s)
	where := askPattern.FindStringIndex(s)
	if where == nil {
		return "", false
	}
	return strings.TrimSpace(s[where[1]:]), true
}
func (r *Router) operator(ctx context.Context, m telegram.Message) {
	if m.ThreadID == 0 {
		return
	}
	topic, e := r.Store.TopicByID(ctx, m.ThreadID)
	if e != nil || topic == nil {
		return
	}
	text := strings.TrimSpace(m.Text)
	if query, yes := ParseAsk(text); yes && topic.UserID > 0 {
		r.ask(ctx, topic.TopicID, topic.UserID, query)
		return
	}
	if topic.ActiveTicketID != nil {
		sent := false
		unsupported := false
		if r.Relay != nil && text != "" {
			sent = r.Relay.ReplyText(ctx, *topic.ActiveTicketID, topic.UserID, text)
		} else if r.Relay != nil && len(m.Photo) > 0 {
			data, mime, key := r.downloadPhoto(ctx, m.Photo)
			if key == "" {
				sent = r.Relay.ReplyPhoto(ctx, *topic.ActiveTicketID, topic.UserID, data, mime, strings.TrimSpace(m.Caption))
			}
		} else if r.Relay != nil {
			unsupported = true
		}
		key := "support.ticket.delivery.failed"
		if sent {
			key = "support.ticket.sent"
		} else if unsupported {
			key = "support.ticket.photo.only"
		}
		r.Sender.Send(ctx, r.Group, m.ThreadID, m.ID, Message(key, *topic.ActiveTicketID))
		return
	}
	if topic.UserID < 0 {
		r.Sender.Send(ctx, r.Group, m.ThreadID, m.ID, Message("support.cabinet_only.no_delivery"))
		return
	}
	if text == "" {
		if r.Sender.Copy(ctx, topic.UserID, r.Group, m.ID, 0) == 0 {
			r.Sender.Send(ctx, topic.UserID, 0, 0, Message("support.fallback.media"))
		}
	} else {
		var mapping *domain.MessageMapping
		if m.ReplyTo != nil {
			mapping, _ = r.Store.MappingByTopicMessage(ctx, m.ThreadID, m.ReplyTo.ID)
		}
		if mapping != nil {
			r.Sender.Send(ctx, mapping.UserChatID, 0, mapping.UserMessageID, text)
		} else {
			r.Sender.Send(ctx, topic.UserID, 0, 0, Message("support.operator.prefix", text))
		}
	}
	r.Sender.Send(ctx, r.Group, m.ThreadID, m.ID, Message("support.sent"))
	r.State.RecordOperator(topic.UserID)
}
func (r *Router) ask(ctx context.Context, topic, user int64, query string) {
	ctx = operationContext(ctx, "operator_ask")
	diagnostics.Payload(ctx, "Operator ask metadata", map[string]any{"user_id": user, "topic_id": topic, "query": query})
	if query == "" {
		r.Sender.Send(ctx, r.Group, topic, 0, Message("support.ask.usage"))
		return
	}
	stop := StartTyping(ctx, r.Sender.API, user)
	defer stop()
	reply, e := r.LLM.Chat(ctx, query, user)
	if e != nil {
		diagnostics.Error(ctx, "Operator ask failed", e)
		r.Sender.Send(ctx, r.Group, topic, 0, Message("support.ask.error", Truncate(e.Error(), 500)))
		return
	}
	text := StripMarker(reply.Text)
	if text == "" {
		text = Message("bot.llm.empty")
	}
	r.Sender.Send(ctx, user, 0, 0, text)
	SendIllustration(ctx, r.Sender, r.State, user, user, reply.FAQContext, text)
	r.State.RecordOperator(user)
	r.Sender.Send(ctx, r.Group, topic, 0, Message("support.ask.header", text))
}
func (r *Router) command(ctx context.Context, m telegram.Message, text string) {
	cmd := strings.Fields(text)[0]
	switch cmd {
	case "/start":
		r.Buffer.Discard(m.From.ID)
		_ = r.History.Clear(ctx, m.From.ID)
		r.State.Clear(m.From.ID)
		r.Sender.Send(ctx, m.Chat.ID, 0, 0, Message("bot.start.welcome"))
		return
	case "/help":
		r.Sender.Send(ctx, m.Chat.ID, 0, 0, Message("bot.help"))
		return
	case "/operator":
		if q := r.State.LastQuery(m.From.ID); q != nil && r.Gaps != nil {
			_ = r.Gaps.EvaluateOperatorRequest(ctx, q.Text, m.From.ID, q.FAQ)
		}
		r.Sender.Send(ctx, m.Chat.ID, 0, 0, Message("bot.operator.transfer"))
		r.Forwarder.Forward(ctx, m.Chat.ID, []int64{m.ID}, m.From, Message("support.operator.request"), true, 0, nil)
		return
	}
	if r.Admins[m.From.ID] && (strings.HasPrefix(text, "/stats") || strings.HasPrefix(text, "/gaps")) {
		r.admin(ctx, m.Chat.ID, text)
		return
	}
	r.Sender.Send(ctx, m.Chat.ID, 0, 0, Message("bot.unknown.command"))
}
func FormatNumber(n int64) string {
	if n < 1000 {
		return strconv.FormatInt(n, 10)
	}
	if n < 1_000_000 {
		return fmt.Sprintf("%.1fK", float64(n)/1000)
	}
	if n < 1_000_000_000 {
		return fmt.Sprintf("%.1fM", float64(n)/1_000_000)
	}
	return fmt.Sprintf("%.1fB", float64(n)/1_000_000_000)
}
func (r *Router) adminName(ctx context.Context, id int64) string {
	u, e := r.Store.UserByID(ctx, id)
	if e == nil && u != nil && strings.TrimSpace(u.Username) != "" {
		return fmt.Sprintf("@%s (%d)", strings.TrimSpace(u.Username), id)
	}
	return strconv.FormatInt(id, 10)
}
func (r *Router) admin(ctx context.Context, chat int64, text string) {
	parts := strings.Fields(text)
	clear := len(parts) == 2 && strings.EqualFold(parts[1], "clear")
	if strings.HasPrefix(text, "/gaps") {
		if r.Gaps == nil {
			return
		}
		if clear {
			n, e := r.Gaps.Clear(ctx)
			if e != nil {
				r.Sender.Send(ctx, chat, 0, 0, Message("bot.clear.failed", Truncate(e.Error(), 300)))
			} else {
				r.Sender.Send(ctx, chat, 0, 0, Message("bot.gaps.cleared", n))
			}
			return
		}
		gaps, e := r.Gaps.Top(ctx, 15)
		if e != nil {
			return
		}
		if len(gaps) == 0 {
			r.Sender.Send(ctx, chat, 0, 0, Message("bot.gaps.empty"))
			return
		}
		lines := []string{Message("bot.gaps.header")}
		for i, g := range gaps {
			reason := g.TriggerReason
			if reason == "" {
				reason = "UNKNOWN"
			}
			lines = append(lines, Message("bot.gaps.row", i+1, g.GapCount, g.UserQuery, reason))
		}
		r.Sender.Send(ctx, chat, 0, 0, strings.Join(lines, "\n"))
		return
	}
	if clear {
		n, e := r.Store.ClearUsage(ctx)
		if e != nil {
			r.Sender.Send(ctx, chat, 0, 0, Message("bot.clear.failed", Truncate(e.Error(), 300)))
		} else {
			r.Sender.Send(ctx, chat, 0, 0, Message("bot.stats.cleared", n))
		}
		return
	}
	limit := 10
	if len(parts) == 2 {
		n, e := strconv.ParseInt(parts[1], 10, 64)
		if e == nil {
			if n > 100 {
				stat, e := r.Store.UsageForUser(ctx, n)
				if e != nil {
					return
				}
				name := r.adminName(ctx, n)
				if stat == nil || stat.RequestCount == 0 {
					r.Sender.Send(ctx, chat, 0, 0, Message("bot.stats.no.data", name))
				} else {
					r.Sender.Send(ctx, chat, 0, 0, Message("bot.stats.user", name, stat.RequestCount, FormatNumber(stat.PromptTokens), FormatNumber(stat.CompletionTokens), FormatNumber(stat.TotalTokens)))
				}
				return
			}
			limit = int(n)
			if limit < 1 {
				limit = 1
			}
		}
	}
	rows, e := r.Store.UsageTop(ctx, limit)
	if e != nil {
		return
	}
	if len(rows) == 0 {
		r.Sender.Send(ctx, chat, 0, 0, Message("bot.stats.empty"))
		return
	}
	lines := []string{Message("bot.stats.top.header", limit)}
	for i, row := range rows {
		lines = append(lines, Message("bot.stats.top.row", i+1, r.adminName(ctx, row.TelegramID), FormatNumber(row.TotalTokens), row.RequestCount))
	}
	r.Sender.Send(ctx, chat, 0, 0, strings.Join(lines, "\n"))
}
