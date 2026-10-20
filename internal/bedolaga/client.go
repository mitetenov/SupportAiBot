package bedolaga

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"net/url"
	"path"
	"strconv"
	"strings"
	"supportbot/internal/diagnostics"
	"supportbot/internal/telegram"
	"sync"
	"time"
	"unicode"
)

const MaxReplyLength = 4000
const MaxMediaBytes = 10 * 1024 * 1024

type TicketMessage struct {
	ID        int64  `json:"id"`
	Text      string `json:"message_text"`
	Admin     bool   `json:"is_from_admin"`
	HasMedia  bool   `json:"has_media"`
	MediaType string `json:"media_type"`
}
type Ticket struct {
	ID       int64           `json:"id"`
	UserID   int64           `json:"user_id"`
	Title    string          `json:"title"`
	Status   string          `json:"status"`
	Priority string          `json:"priority"`
	Messages []TicketMessage `json:"messages"`
}

func (t Ticket) Last() *TicketMessage {
	if len(t.Messages) == 0 {
		return nil
	}
	m := t.Messages[len(t.Messages)-1]
	return &m
}
func (t Ticket) LastUser() *TicketMessage {
	for i := len(t.Messages) - 1; i >= 0; i-- {
		if !t.Messages[i].Admin {
			m := t.Messages[i]
			return &m
		}
	}
	return nil
}
func OpenStatus(s string) bool { return s == "open" || s == "pending" }
func (t Ticket) Awaits() bool  { m := t.Last(); return m != nil && !m.Admin && OpenStatus(t.Status) }
func (t Ticket) Question(m *TicketMessage) string {
	text := ""
	if m != nil {
		text = strings.TrimSpace(m.Text)
	}
	title := strings.TrimSpace(t.Title)
	if text == "" {
		return title
	}
	if len(t.Messages) == 1 && title != "" && !strings.Contains(strings.ToLower(text), strings.ToLower(title)) {
		return title + "\n\n" + text
	}
	return text
}

type Lookup struct {
	Known      bool
	TelegramID *int64
	User       telegram.User
}
type PostedReply struct{ MessageID *int64 }
type Client struct {
	BaseURL, Key string
	HTTP         *http.Client
	mu           sync.Mutex
	lookups      map[int64]Lookup
}

func NewClient(base, key string, h *http.Client) *Client {
	if h == nil {
		h = &http.Client{Timeout: 30 * time.Second}
	}
	return &Client{BaseURL: strings.TrimRight(base, "/"), Key: key, HTTP: h, lookups: map[int64]Lookup{}}
}
func (c *Client) request(ctx context.Context, method, route string, payload any) (status int, data []byte, failure error) {
	ctx = diagnostics.WithRequest(diagnostics.WithOperation(ctx, "bedolaga", method), diagnostics.NewID())
	diagnostics.Payload(ctx, "Bedolaga HTTP request", map[string]any{"route": route, "body": payload})
	defer func() {
		if failure != nil || status >= 400 {
			diagnostics.Error(ctx, "Bedolaga HTTP request failed", failure, "http_status", status)
		}
		diagnostics.Payload(ctx, "Bedolaga HTTP response", data, "http_status", status)
	}()
	var body io.Reader
	if payload != nil {
		b, e := json.Marshal(payload)
		if e != nil {
			return 0, nil, e
		}
		body = bytes.NewReader(b)
	}
	req, e := http.NewRequestWithContext(ctx, method, c.BaseURL+route, body)
	if e != nil {
		return 0, nil, errors.New("invalid panel endpoint")
	}
	req.Header.Set("X-API-Key", c.Key)
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, e := c.HTTP.Do(req)
	if e != nil {
		return 0, nil, errors.New("panel transport request failed")
	}
	defer resp.Body.Close()
	b, e := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	return resp.StatusCode, b, e
}
func (c *Client) GetTicket(ctx context.Context, id int64) *Ticket {
	status, b, e := c.request(ctx, "GET", fmt.Sprintf("/tickets/%d", id), nil)
	if e != nil || status != 200 {
		return nil
	}
	var ticket Ticket
	if json.Unmarshal(b, &ticket) != nil || ticket.ID == 0 {
		diagnostics.Error(ctx, "Bedolaga ticket response malformed", nil)
		return nil
	}
	return &ticket
}
func (c *Client) List(ctx context.Context, limit int) []int64 {
	ids := []int64{}
	for _, status := range []string{"open", "pending"} {
		code, b, e := c.request(ctx, "GET", fmt.Sprintf("/tickets?status=%s&limit=%d", status, limit), nil)
		if e != nil || code != 200 {
			continue
		}
		var rows []map[string]json.RawMessage
		if json.Unmarshal(b, &rows) != nil {
			var envelope struct {
				Items []map[string]json.RawMessage `json:"items"`
			}
			if json.Unmarshal(b, &envelope) != nil {
				continue
			}
			rows = envelope.Items
		}
		for _, row := range rows {
			if id, ok := parseID(row["id"]); ok {
				ids = append(ids, id)
			}
		}
	}
	return ids
}
func parseID(b json.RawMessage) (int64, bool) {
	var n int64
	if json.Unmarshal(b, &n) == nil {
		return n, true
	}
	var s string
	if json.Unmarshal(b, &s) == nil {
		n, e := strconv.ParseInt(s, 10, 64)
		return n, e == nil
	}
	return 0, false
}
func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}
func (c *Client) postReply(ctx context.Context, id int64, payload any) *PostedReply {
	code, b, e := c.request(ctx, "POST", fmt.Sprintf("/tickets/%d/reply", id), payload)
	if e != nil || (code != 200 && code != 201) {
		return nil
	}
	posted := &PostedReply{}
	var raw struct {
		Message struct {
			ID json.RawMessage `json:"id"`
		} `json:"message"`
	}
	if json.Unmarshal(b, &raw) == nil {
		if value, ok := parseID(raw.Message.ID); ok {
			posted.MessageID = &value
		}
	}
	if posted.MessageID == nil {
		diagnostics.Error(ctx, "Bedolaga accepted reply omitted message ID", nil)
	}
	return posted
}
func (c *Client) Reply(ctx context.Context, id int64, text string) *PostedReply {
	return c.postReply(ctx, id, map[string]any{"message_text": truncate(text, MaxReplyLength)})
}
func (c *Client) ReplyPhoto(ctx context.Context, id int64, text, b64, mime string) *PostedReply {
	ctx = diagnostics.WithRequest(diagnostics.WithOperation(ctx, "bedolaga", "upload_photo"), diagnostics.NewID())
	diagnostics.Payload(ctx, "Bedolaga photo upload metadata", map[string]any{"ticket_id": id, "mime_type": mime, "encoded_size": len(b64)})
	content, e := base64.StdEncoding.Strict().DecodeString(b64)
	if e != nil || len(content) == 0 || len(content) > MaxMediaBytes {
		diagnostics.Error(ctx, "Bedolaga photo upload rejected", e)
		return nil
	}
	ext := "jpg"
	switch strings.ToLower(mime) {
	case "image/png":
		ext = "png"
	case "image/webp":
		ext = "webp"
	case "image/gif":
		ext = "gif"
	}
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	_ = mw.WriteField("media_type", "photo")
	header := textproto.MIMEHeader{}
	header.Set("Content-Disposition", fmt.Sprintf(`form-data; name="file"; filename="support-photo.%s"`, ext))
	header.Set("Content-Type", mime)
	p, e := mw.CreatePart(header)
	if e != nil {
		return nil
	}
	_, _ = p.Write(content)
	_ = mw.Close()
	req, e := http.NewRequestWithContext(ctx, "POST", c.BaseURL+"/upload", &buf)
	if e != nil {
		return nil
	}
	req.Header.Set("X-API-Key", c.Key)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	resp, e := c.HTTP.Do(req)
	if e != nil {
		diagnostics.Error(ctx, "Bedolaga photo upload failed", e)
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 && resp.StatusCode != 201 {
		diagnostics.Error(ctx, "Bedolaga photo upload rejected", nil, "http_status", resp.StatusCode)
		return nil
	}
	var upload struct {
		ID   string `json:"file_id"`
		Type string `json:"media_type"`
	}
	if json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&upload) != nil || strings.TrimSpace(upload.ID) == "" {
		return nil
	}
	if upload.Type == "" {
		upload.Type = "photo"
	}
	caption := truncate(text, MaxReplyLength)
	var value any = caption
	if caption == "" {
		value = nil
	}
	return c.postReply(ctx, id, map[string]any{"message_text": value, "media_caption": value, "media_type": upload.Type, "media_file_id": upload.ID})
}
func (c *Client) Priority(ctx context.Context, id int64, priority string) bool {
	for attempt := 0; attempt < 3; attempt++ {
		code, _, e := c.request(ctx, "POST", fmt.Sprintf("/tickets/%d/priority", id), map[string]any{"priority": priority})
		if e == nil && code == 200 {
			return true
		}
		if e == nil && code != 408 && code != 429 && code < 500 {
			return false
		}
		if attempt < 2 {
			select {
			case <-ctx.Done():
				return false
			case <-time.After(time.Duration(1<<attempt) * 500 * time.Millisecond):
			}
		}
	}
	return false
}
func (c *Client) Resolve(ctx context.Context, id int64) Lookup {
	c.mu.Lock()
	cached, ok := c.lookups[id]
	c.mu.Unlock()
	if ok {
		return cached
	}
	code, b, e := c.request(ctx, "GET", fmt.Sprintf("/users/%d", id), nil)
	if e != nil || code != 200 {
		return Lookup{}
	}
	var profile map[string]json.RawMessage
	if json.Unmarshal(b, &profile) != nil || profile == nil {
		return Lookup{}
	}
	var u telegram.User
	_ = json.Unmarshal(b, &u)
	var email string
	_ = json.Unmarshal(profile["email"], &email)
	u.Email = strings.TrimSpace(email)
	u.Username = strings.TrimSpace(u.Username)
	u.FirstName = strings.TrimSpace(u.FirstName)
	u.LastName = strings.TrimSpace(u.LastName)
	r := Lookup{Known: true, User: u}
	raw := profile["telegram_id"]
	if len(raw) > 0 && string(raw) != "null" {
		v, valid := parseID(raw)
		if !valid {
			return Lookup{}
		}
		r.TelegramID = &v
		if v != 0 {
			c.mu.Lock()
			c.lookups[id] = r
			c.mu.Unlock()
		}
	}
	return r
}
func (c *Client) ResolveMediaURL(raw string) string {
	base, e := url.Parse(c.BaseURL)
	if e != nil {
		return ""
	}
	target, e := url.Parse(raw)
	if e != nil {
		return ""
	}
	if target.Scheme == "" && target.Host == "" {
		target.Scheme = base.Scheme
		target.Host = base.Host
		target.Path = strings.TrimRight(base.Path, "/") + "/" + strings.TrimLeft(target.Path, "/")
	}
	if target.Scheme != base.Scheme || telegram.Origin(target) != telegram.Origin(base) || target.User != nil {
		return ""
	}
	return target.String()
}
func safeFilename(raw, kind string, ticket, message int64) string {
	raw = path.Base(strings.ReplaceAll(raw, "\\", "/"))
	raw = strings.TrimSpace(strings.Map(func(r rune) rune {
		if r < 32 || !unicode.IsPrint(r) {
			return -1
		}
		return r
	}, raw))
	if raw != "" && raw != "." && raw != "/" {
		return raw
	}
	ext := map[string]string{"photo": ".jpg", "video": ".mp4", "video_note": ".mp4", "animation": ".gif", "voice": ".ogg", "audio": ".mp3"}[kind]
	if ext == "" {
		ext = ".bin"
	}
	return fmt.Sprintf("ticket-%d-message-%d%s", ticket, message, ext)
}
func (c *Client) DescribeMedia(ctx context.Context, ticket, message int64, fallback string) *telegram.Media {
	code, b, e := c.request(ctx, "GET", fmt.Sprintf("/tickets/%d/messages/%d/media", ticket, message), nil)
	if e != nil || code != 200 {
		return nil
	}
	var raw map[string]json.RawMessage
	if json.Unmarshal(b, &raw) != nil {
		return nil
	}
	stringField := func(k string) string { var v string; _ = json.Unmarshal(raw[k], &v); return strings.TrimSpace(v) }
	address := c.ResolveMediaURL(stringField("media_url"))
	if address == "" {
		return nil
	}
	kind := stringField("media_type")
	if kind == "" {
		kind = fallback
	}
	if kind == "" {
		kind = "document"
	}
	filename := stringField("file_name")
	if filename == "" {
		filename = stringField("filename")
	}
	var size *int64
	if v := raw["file_size"]; len(v) > 0 && string(v) != "null" {
		n, ok := parseID(v)
		if !ok || n < 0 {
			return nil
		}
		size = &n
	}
	return &telegram.Media{Type: kind, URL: address, Filename: safeFilename(filename, kind, ticket, message), MIME: stringField("mime_type"), Size: size, Headers: map[string]string{"X-API-Key": c.Key}}
}
func (c *Client) Image(ctx context.Context, media *telegram.Media) (data string, mimeType string) {
	ctx = diagnostics.WithRequest(diagnostics.WithOperation(ctx, "bedolaga", "download_image"), diagnostics.NewID())
	defer func() {
		if data == "" {
			diagnostics.Error(ctx, "Bedolaga image unavailable", nil)
		}
		diagnostics.Payload(ctx, "Bedolaga image download metadata", map[string]any{"encoded_size": len(data), "mime_type": mimeType})
	}()
	if media == nil {
		return "", ""
	}
	req, e := http.NewRequestWithContext(ctx, "GET", media.URL, nil)
	if e != nil {
		return "", ""
	}
	for k, v := range media.Headers {
		req.Header.Set(k, v)
	}
	h := *c.HTTP
	h.CheckRedirect = func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }
	resp, e := h.Do(req)
	if e != nil {
		return "", ""
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 || resp.ContentLength > MaxMediaBytes {
		return "", ""
	}
	b, e := io.ReadAll(io.LimitReader(resp.Body, MaxMediaBytes+1))
	if e != nil || len(b) == 0 || len(b) > MaxMediaBytes {
		return "", ""
	}
	mime := strings.TrimSpace(strings.Split(resp.Header.Get("Content-Type"), ";")[0])
	if mime == "" {
		mime = "image/jpeg"
	}
	return base64.StdEncoding.EncodeToString(b), mime
}
