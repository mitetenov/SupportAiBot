package bot

import (
	"context"
	"fmt"
	"path/filepath"
	"strconv"
	"strings"
	"supportbot/internal/diagnostics"
	"supportbot/internal/domain"
	"supportbot/internal/telegram"
	"time"
)

func Truncate(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}
func UserName(u *telegram.User) string {
	if u == nil {
		return "Unknown"
	}
	if strings.TrimSpace(u.Username) != "" {
		return "@" + strings.TrimSpace(u.Username)
	}
	if name := strings.TrimSpace(u.FirstName + " " + u.LastName); name != "" {
		return name
	}
	id := u.ID
	if id < 0 {
		id = -id
	}
	return fmt.Sprintf("User %d", id)
}
func TopicName(u *telegram.User) string {
	id := u.ID
	if id < 0 {
		id = -id
	}
	identifier := strconv.FormatInt(id, 10)
	if u.ID < 0 && strings.TrimSpace(u.Email) != "" {
		identifier = strings.TrimSpace(u.Email)
	}
	if len([]rune(identifier)) > 118 {
		identifier = Truncate(identifier, 117) + "…"
	}
	if strings.TrimSpace(u.Username) == "" && strings.TrimSpace(u.FirstName+u.LastName) == "" {
		return Truncate("User "+identifier, 128)
	}
	suffix := " (ID: " + identifier + ")"
	return Truncate(UserName(u), 128-len([]rune(suffix))) + suffix
}

type Forwarder struct {
	Sender *telegram.Sender
	Store  domain.Store
	Group  int64
	Admin  string
	locks  KeyedLock
}

func (f *Forwarder) Topic(ctx context.Context, u *telegram.User, recreate int64) int64 {
	unlock, err := f.locks.Lock(ctx, u.ID)
	if err != nil {
		diagnostics.Error(ctx, "Support topic resolution failed", err)
		return 0
	}
	defer unlock()
	existing, err := f.Store.TopicByUser(ctx, u.ID)
	if err != nil {
		diagnostics.Error(ctx, "Support topic resolution failed", err)
		return 0
	}
	if existing != nil && recreate != 0 && existing.TopicID == recreate {
		if f.Store.DeleteTopic(ctx, u.ID, recreate) != nil {
			return 0
		}
		existing = nil
	}
	name := TopicName(u)
	if existing != nil {
		if u.ID < 0 && existing.UserName != name {
			if f.Sender.API.Call(ctx, "editForumTopic", map[string]any{"chat_id": f.Group, "message_thread_id": existing.TopicID, "name": name}, nil) == nil {
				existing.UserName = name
				observeError(ctx, "Support topic rename recording failed", f.Store.SaveTopic(ctx, *existing))
			}
		}
		return existing.TopicID
	}
	var created struct {
		ID int64 `json:"message_thread_id"`
	}
	if f.Sender.API.Call(ctx, "createForumTopic", map[string]any{"chat_id": f.Group, "name": name}, &created) != nil || created.ID == 0 {
		return 0
	}
	stored := UserName(u)
	if u.ID < 0 {
		stored = name
	}
	if f.Store.SaveTopic(ctx, domain.Topic{UserID: u.ID, TopicID: created.ID, UserName: stored, CreatedAt: time.Now().UTC()}) != nil {
		return 0
	}
	return created.ID
}
func (f *Forwarder) copy(ctx context.Context, chat, message, topic int64) bool {
	id := f.Sender.Copy(ctx, f.Group, chat, message, topic)
	if id == 0 {
		return false
	}
	observeError(ctx, "Copied message mapping recording failed", f.Store.SaveMessageMapping(ctx, domain.MessageMapping{TopicMessageID: id, TopicID: topic, UserChatID: chat, UserMessageID: message}))
	return true
}
func (f *Forwarder) Forward(ctx context.Context, chat int64, ids []int64, u *telegram.User, response string, escalate bool, picture int64, ticket *int64) int64 {
	diagnostics.Payload(ctx, "Support forward metadata", map[string]any{"chat_id": chat, "message_ids": ids, "escalate": escalate, "ticket_id": ticket})
	if u == nil {
		return 0
	}
	topic := f.Topic(ctx, u, 0)
	if topic == 0 {
		return 0
	}
	if len(ids) > 0 {
		if !f.copy(ctx, chat, ids[0], topic) {
			topic = f.Topic(ctx, u, topic)
			if topic == 0 || !f.copy(ctx, chat, ids[0], topic) {
				return 0
			}
		}
		for _, id := range ids[1:] {
			f.copy(ctx, chat, id, topic)
		}
	}
	prefix := ""
	if escalate && f.Admin != "" {
		prefix = "@" + strings.TrimLeft(f.Admin, "@") + " "
	}
	if len([]rune(response)) > 3500 {
		response = Truncate(response, 3500) + "...\n\n" + Message("admin.response.truncated")
	}
	f.Sender.Send(ctx, f.Group, topic, 0, prefix+Message("admin.response.prefix")+" "+UserName(u)+":\n\n"+response)
	if picture != 0 {
		f.copy(ctx, chat, picture, topic)
	}
	observeError(ctx, "Active ticket update failed", f.Store.SetActiveTicket(ctx, u.ID, ticket))
	return topic
}
func (f *Forwarder) ForwardMedia(ctx context.Context, u *telegram.User, ticket int64, media *telegram.Media) int64 {
	topic := f.Topic(ctx, u, 0)
	if topic == 0 {
		return 0
	}
	observeError(ctx, "Active ticket update failed", f.Store.SetActiveTicket(ctx, u.ID, &ticket))
	if media == nil || f.Sender.TicketMedia(ctx, f.Group, topic, *media, Message("bedolaga.media.caption", ticket)) == 0 {
		f.Sender.Send(ctx, f.Group, topic, 0, Message("bedolaga.media.forward.failed", ticket))
		return 0
	}
	return topic
}
func (f *Forwarder) Error(ctx context.Context, u *telegram.User, query, visible, details string) {
	if u == nil {
		return
	}
	topic := f.Topic(ctx, u, 0)
	if topic == 0 {
		return
	}
	observeError(ctx, "Active ticket clear failed", f.Store.SetActiveTicket(ctx, u.ID, nil))
	q := Truncate(query, 300)
	if q != query {
		q += "..."
	}
	f.Sender.Send(ctx, f.Group, topic, 0, Message("admin.error.prefix")+" "+UserName(u)+": "+q+"\n\nБот ответил:\n"+visible)
	prefix := ""
	if f.Admin != "" {
		prefix = "@" + strings.TrimLeft(f.Admin, "@") + " "
	}
	f.Sender.Send(ctx, f.Group, topic, 0, prefix+Message("admin.error.details")+"\n\n"+Truncate(details, 3500))
}
func SendIllustration(ctx context.Context, sender *telegram.Sender, state *ConversationState, chat, user int64, faq domain.FAQContext, answer string) int64 {
	if len(faq.Results) == 0 {
		return 0
	}
	hit := faq.Results[0]
	if hit.Image == "" || state.PictureSeen(user, hit.Image) || strings.TrimSpace(hit.Answer) == "" {
		return 0
	}
	normalize := func(s string) string { return strings.Join(strings.Fields(strings.ToLower(s)), " ") }
	if !strings.Contains(normalize(answer), normalize(hit.Answer)) {
		return 0
	}
	root, err := filepath.Abs("faq/images")
	if err != nil {
		diagnostics.Error(ctx, "Support topic resolution failed", err)
		return 0
	}
	candidate, err := filepath.Abs(filepath.Join(root, hit.Image))
	if err != nil || !strings.HasPrefix(candidate, root+string(filepath.Separator)) {
		return 0
	}
	id := sender.Photo(ctx, chat, 0, candidate)
	if id != 0 {
		state.RecordPicture(user, hit.Image)
	}
	return id
}
