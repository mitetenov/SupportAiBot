// Package telegram implements the Telegram Bot API transport without provider dependencies.
package telegram

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"supportbot/internal/diagnostics"
	"time"
)

type User struct {
	ID        int64  `json:"id"`
	IsBot     bool   `json:"is_bot"`
	Username  string `json:"username"`
	FirstName string `json:"first_name"`
	LastName  string `json:"last_name"`
	Email     string `json:"-"`
}
type Chat struct {
	ID   int64  `json:"id"`
	Type string `json:"type"`
}
type File struct {
	ID   string `json:"file_id"`
	Path string `json:"file_path"`
	Size int64  `json:"file_size"`
	Name string `json:"file_name"`
	MIME string `json:"mime_type"`
}
type Message struct {
	ID       int64    `json:"message_id"`
	ThreadID int64    `json:"message_thread_id"`
	From     *User    `json:"from"`
	Chat     Chat     `json:"chat"`
	Text     string   `json:"text"`
	Caption  string   `json:"caption"`
	Photo    []File   `json:"photo"`
	Document *File    `json:"document"`
	ReplyTo  *Message `json:"reply_to_message"`
}
type Reaction struct {
	Chat      Chat              `json:"chat"`
	MessageID int64             `json:"message_id"`
	User      *User             `json:"user"`
	New       []json.RawMessage `json:"new_reaction"`
}
type Callback struct {
	ID      string   `json:"id"`
	From    User     `json:"from"`
	Message *Message `json:"message"`
	Data    string   `json:"data"`
}
type Update struct {
	ID       int64     `json:"update_id"`
	Message  *Message  `json:"message"`
	Reaction *Reaction `json:"message_reaction"`
	Callback *Callback `json:"callback_query"`
}
type API interface {
	Call(context.Context, string, any, any) error
	Upload(context.Context, string, map[string]string, string, string, io.Reader, any) error
	Download(context.Context, string, int64) ([]byte, error)
}
type Client struct {
	Token   string
	BaseURL string
	HTTP    *http.Client
}

func New(token string, h *http.Client) *Client {
	if h == nil {
		h = &http.Client{Timeout: 150 * time.Second}
	}
	return &Client{Token: token, BaseURL: "https://api.telegram.org", HTTP: h}
}

type APIError struct {
	Code        int
	Description string
}

func (e *APIError) Error() string { return fmt.Sprintf("Telegram API %d: %s", e.Code, e.Description) }
func (c *Client) endpoint(method string) string {
	return strings.TrimRight(c.BaseURL, "/") + "/bot" + c.Token + "/" + method
}
func (c *Client) perform(req *http.Request, out any) (failure error) {
	status := 0
	started := time.Now()
	defer func() {
		if failure != nil {
			diagnostics.Error(req.Context(), "Telegram API request failed", failure, "http_status", status)
		}
		diagnostics.Payload(req.Context(), "Telegram HTTP outcome", map[string]any{"status": status, "duration_ms": time.Since(started).Milliseconds()})
	}()
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return errors.New("Telegram transport request failed")
	}
	defer resp.Body.Close()
	status = resp.StatusCode
	var result struct {
		OK          bool            `json:"ok"`
		Result      json.RawMessage `json:"result"`
		Code        int             `json:"error_code"`
		Description string          `json:"description"`
	}
	if err = json.NewDecoder(io.LimitReader(resp.Body, 16<<20)).Decode(&result); err != nil {
		return errors.New("Telegram response is not valid JSON")
	}
	diagnostics.Payload(req.Context(), "Telegram API response", result)
	if !result.OK {
		return &APIError{result.Code, result.Description}
	}
	if out != nil {
		return json.Unmarshal(result.Result, out)
	}
	return nil
}
func (c *Client) Call(ctx context.Context, method string, payload any, out any) error {
	ctx = diagnostics.WithRequest(diagnostics.WithOperation(ctx, "telegram", method), diagnostics.NewID())
	diagnostics.Payload(ctx, "Telegram API request", payload)
	b, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint(method), bytes.NewReader(b))
	if err != nil {
		return errors.New("invalid Telegram API endpoint")
	}
	req.Header.Set("Content-Type", "application/json")
	return c.perform(req, out)
}
func (c *Client) Upload(ctx context.Context, method string, fields map[string]string, field, filename string, content io.Reader, out any) error {
	ctx = diagnostics.WithRequest(diagnostics.WithOperation(ctx, "telegram", method), diagnostics.NewID())
	diagnostics.Payload(ctx, "Telegram multipart request metadata", map[string]any{"fields": fields, "field": field, "filename": filename})
	reader, writer := io.Pipe()
	mw := multipart.NewWriter(writer)
	go func() {
		var err error
		defer func() {
			if err != nil {
				_ = writer.CloseWithError(err)
			} else {
				_ = writer.Close()
			}
		}()
		for key, value := range fields {
			if err = mw.WriteField(key, value); err != nil {
				return
			}
		}
		var part io.Writer
		part, err = mw.CreateFormFile(field, filename)
		if err != nil {
			return
		}
		_, err = io.Copy(part, content)
		if err != nil {
			return
		}
		err = mw.Close()
	}()
	defer reader.Close()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint(method), reader)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", mw.FormDataContentType())
	return c.perform(req, out)
}
func (c *Client) Download(ctx context.Context, path string, maxBytes int64) (data []byte, failure error) {
	ctx = diagnostics.WithRequest(diagnostics.WithOperation(ctx, "telegram", "download"), diagnostics.NewID())
	diagnostics.Payload(ctx, "Telegram file download metadata", map[string]any{"file_path": path, "max_bytes": maxBytes})
	defer func() {
		if failure != nil {
			diagnostics.Error(ctx, "Telegram file download failed", failure)
		}
		diagnostics.Payload(ctx, "Telegram file download outcome", map[string]any{"byte_size": len(data)})
	}()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(c.BaseURL, "/")+"/file/bot"+c.Token+"/"+strings.TrimLeft(path, "/"), nil)
	if err != nil {
		return nil, errors.New("invalid Telegram file endpoint")
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, errors.New("Telegram file download failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("Telegram file status %d", resp.StatusCode)
	}
	if maxBytes > 0 && resp.ContentLength > maxBytes {
		return nil, ErrTooLarge
	}
	limit := maxBytes
	if limit <= 0 {
		limit = 50 << 20
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return nil, errors.New("Telegram file read failed")
	}
	if int64(len(b)) > limit {
		return nil, ErrTooLarge
	}
	return b, nil
}

var ErrTooLarge = errors.New("attachment exceeds size limit")

func Origin(u *url.URL) string {
	port := u.Port()
	if port == "" {
		if u.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}
	return strings.ToLower(u.Scheme + "://" + u.Hostname() + ":" + port)
}

// OpenMedia strips credentials on any origin change, including redirects back.
func OpenMedia(ctx context.Context, h *http.Client, address string, headers map[string]string) (io.ReadCloser, error) {
	initial, err := url.Parse(address)
	if err != nil {
		return nil, errors.New("invalid media URL")
	}
	copyClient := *h
	copyClient.CheckRedirect = func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }
	current := initial
	for i := 0; i <= 5; i++ {
		req, err := http.NewRequestWithContext(ctx, "GET", current.String(), nil)
		if err != nil {
			return nil, errors.New("invalid media request")
		}
		if Origin(initial) == Origin(current) {
			for k, v := range headers {
				req.Header.Set(k, v)
			}
		}
		resp, err := copyClient.Do(req)
		if err != nil {
			return nil, errors.New("media download failed")
		}
		if resp.StatusCode == 301 || resp.StatusCode == 302 || resp.StatusCode == 303 || resp.StatusCode == 307 || resp.StatusCode == 308 {
			resp.Body.Close()
			location := resp.Header.Get("Location")
			if location == "" {
				return nil, errors.New("redirect without Location")
			}
			target, err := url.Parse(location)
			if err != nil {
				return nil, errors.New("invalid redirect")
			}
			current = current.ResolveReference(target)
			continue
		}
		if resp.StatusCode != 200 {
			resp.Body.Close()
			return nil, fmt.Errorf("media HTTP %d", resp.StatusCode)
		}
		return resp.Body, nil
	}
	return nil, errors.New("too many media redirects")
}
func Fields(chatID, threadID int64) map[string]string {
	m := map[string]string{"chat_id": strconv.FormatInt(chatID, 10)}
	if threadID != 0 {
		m["message_thread_id"] = strconv.FormatInt(threadID, 10)
	}
	return m
}
