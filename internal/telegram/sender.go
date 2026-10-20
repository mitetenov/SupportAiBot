package telegram

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"io"
	"supportbot/internal/diagnostics"

	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

type Media struct {
	Type     string
	URL      string
	Filename string
	MIME     string
	Size     *int64
	Headers  map[string]string
}
type Sender struct {
	API     API
	HTTP    *http.Client
	mu      sync.Mutex
	fileIDs map[string]string
}

func NewSender(api API) *Sender {
	return &Sender{API: api, HTTP: &http.Client{}, fileIDs: map[string]string{}}
}
func (s *Sender) Send(ctx context.Context, chat, thread, reply int64, text string) {
	if strings.TrimSpace(text) == "" {
		return
	}
	for i, chunk := range SplitHTML(MarkdownHTML(text), 4096) {
		m := map[string]any{"chat_id": chat, "text": chunk, "parse_mode": "HTML"}
		if thread != 0 {
			m["message_thread_id"] = thread
		}
		if i == 0 && reply != 0 {
			m["reply_to_message_id"] = reply
		}
		err := s.API.Call(ctx, "sendMessage", m, nil)
		if err != nil && parseError(err) {
			m["text"] = StripHTML(chunk)
			delete(m, "parse_mode")
			err = s.API.Call(ctx, "sendMessage", m, nil)
		}
		if err != nil {
			diagnostics.Error(ctx, "Telegram message sending failed", err)
		}
	}
}
func parseError(err error) bool {
	v := strings.ToLower(err.Error())
	var ae *APIError
	return strings.Contains(v, "parse entities") || (errors.As(err, &ae) && ae.Code == 400 && strings.Contains(v, "entity"))
}
func (s *Sender) Copy(ctx context.Context, chat, from, message, thread int64) int64 {
	m := map[string]any{"chat_id": chat, "from_chat_id": from, "message_id": message}
	if thread != 0 {
		m["message_thread_id"] = thread
	}
	var sent struct {
		ID int64 `json:"message_id"`
	}
	if err := s.API.Call(ctx, "copyMessage", m, &sent); err != nil {
		diagnostics.Error(ctx, "Telegram message copy failed", err)
		return 0
	}
	return sent.ID
}
func (s *Sender) Reaction(ctx context.Context, chat, message int64, reactions any) {
	if err := s.API.Call(ctx, "setMessageReaction", map[string]any{"chat_id": chat, "message_id": message, "reaction": reactions}, nil); err != nil {
		diagnostics.Error(ctx, "Telegram reaction failed", err)
	}
}
func (s *Sender) Photo(ctx context.Context, chat, thread int64, path string) int64 {
	s.mu.Lock()
	id := s.fileIDs[path]
	s.mu.Unlock()
	var sent Message
	var err error
	if id != "" {
		m := map[string]any{"chat_id": chat, "photo": id}
		if thread != 0 {
			m["message_thread_id"] = thread
		}
		err = s.API.Call(ctx, "sendPhoto", m, &sent)
	} else {
		file, e := os.Open(path)
		if e != nil {
			return 0
		}
		defer file.Close()
		err = s.API.Upload(ctx, "sendPhoto", Fields(chat, thread), "photo", filepath.Base(path), file, &sent)
	}
	if err != nil {
		diagnostics.Error(ctx, "Telegram media delivery failed", err)
		return 0
	}
	if len(sent.Photo) > 0 {
		s.mu.Lock()
		s.fileIDs[path] = sent.Photo[len(sent.Photo)-1].ID
		s.mu.Unlock()
	}
	return sent.ID
}
func (s *Sender) PhotoBytes(ctx context.Context, chat, thread int64, b64, mime, caption string) int64 {
	b, e := base64.StdEncoding.DecodeString(b64)
	if e != nil || len(b) == 0 {
		return 0
	}
	extension := "jpg"
	switch strings.ToLower(mime) {
	case "image/png":
		extension = "png"
	case "image/webp":
		extension = "webp"
	case "image/gif":
		extension = "gif"
	}
	fields := Fields(chat, thread)
	if caption != "" {
		fields["caption"] = caption
	}
	var sent Message
	if s.API.Upload(ctx, "sendPhoto", fields, "photo", "ticket-photo."+extension, bytes.NewReader(b), &sent) != nil {
		return 0
	}
	return sent.ID
}
func (s *Sender) TicketMedia(ctx context.Context, chat, thread int64, media Media, caption string) int64 {
	if media.Size != nil && *media.Size > 50<<20 {
		return 0
	}
	kind := "document"
	if media.Type == "photo" {
		kind = "photo"
	} else if (media.Type == "video" || media.Type == "video_note") && (media.MIME == "video/mp4" || strings.HasSuffix(strings.ToLower(media.Filename), ".mp4") || media.MIME == "") {
		kind = "video"
	}
	send := func(k string) (int64, error) {
		body, e := OpenMedia(ctx, s.HTTP, media.URL, media.Headers)
		if e != nil {
			return 0, e
		}
		defer body.Close()
		fields := Fields(chat, thread)
		if caption != "" {
			fields["caption"] = caption
		}
		if k == "video" {
			fields["supports_streaming"] = "true"
		}
		var out Message
		e = s.API.Upload(ctx, "send"+strings.ToUpper(k[:1])+k[1:], fields, k, media.Filename, &boundedMediaReader{reader: body, remaining: 50 << 20}, &out)
		return out.ID, e
	}
	id, e := send(kind)
	var ae *APIError
	if e != nil && kind == "video" && errors.As(e, &ae) && ae.Code == 400 {
		id, e = send("document")
	}
	if e != nil {
		diagnostics.Error(ctx, "Telegram media upload failed", e)
		return 0
	}
	return id
}
func (s *Sender) Edit(ctx context.Context, chat, message int64, text string) bool {
	m := map[string]any{"chat_id": chat, "message_id": message, "text": MarkdownHTML(text), "parse_mode": "HTML"}
	e := s.API.Call(ctx, "editMessageText", m, nil)
	if e != nil && parseError(e) {
		m["text"] = StripHTML(m["text"].(string))
		delete(m, "parse_mode")
		e = s.API.Call(ctx, "editMessageText", m, nil)
	}
	if e != nil {
		diagnostics.Error(ctx, "Telegram message edit failed", e)
	}
	return e == nil
}

type boundedMediaReader struct {
	reader    io.Reader
	remaining int64
}

func (r *boundedMediaReader) Read(p []byte) (int, error) {
	if r.remaining == 0 {
		var probe [1]byte
		n, e := r.reader.Read(probe[:])
		if n > 0 {
			return 0, ErrTooLarge
		}
		return 0, e
	}
	if int64(len(p)) > r.remaining {
		p = p[:r.remaining]
	}
	n, e := r.reader.Read(p)
	r.remaining -= int64(n)
	return n, e
}
