// Package llm implements the provider wire protocols without changing the bot's prompts.
package llm

import (
	"bytes"
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"supportbot/internal/diagnostics"
	"supportbot/internal/domain"
	"supportbot/internal/mcp"
	"time"
)

//go:embed system_prompt.txt
var SystemPrompt string

type Object = map[string]any
type ProviderConfig struct {
	Name, Model, BaseURL, APIKey, ReasoningEffort string
	Temperature                                   *float64
	Timeout                                       time.Duration
}
type ToolCall struct {
	Name, ID         string
	Arguments        Object
	ThoughtSignature any
}
type Response struct {
	Text             string
	Calls            []ToolCall
	Raw              []any
	Reasoning        *string
	ReasoningDetails []any
}
type ProcessingError struct {
	Message, Friendly  string
	Status             int
	Fallback           bool
	ToolOutcomeUnknown bool
	Cause              error
}

func (e *ProcessingError) Error() string       { return e.Message }
func (e *ProcessingError) UserMessage() string { return e.Friendly }
func (e *ProcessingError) Unwrap() error       { return e.Cause }

const defaultUserError = "Произошла ошибка при обработке запроса. Попробуйте позже."

func processing(s string) *ProcessingError {
	return &ProcessingError{Message: s, Friendly: defaultUserError}
}
func parseError(s string) *ProcessingError {
	return &ProcessingError{Message: s, Friendly: "Ошибка обработки ответа модели."}
}
func fallbackStatus(n int) bool {
	switch n {
	case 401, 402, 403, 408, 413, 429, 500, 502, 503, 504:
		return true
	}
	return false
}
func balance(s string) bool {
	s = strings.ToLower(s)
	for _, p := range []string{"insufficient balance", "insufficient credit", "insufficient quota", "quota exceeded", "credit balance"} {
		if strings.Contains(s, p) {
			return true
		}
	}
	return false
}
func FallbackEligible(err error) bool {
	var p *ProcessingError
	if errors.As(err, &p) {
		if p.ToolOutcomeUnknown {
			return false
		}
		return p.Fallback || fallbackStatus(p.Status) || balance(p.Message) || FallbackEligible(p.Cause)
	}
	if err == nil || errors.Is(err, context.Canceled) {
		return false
	}
	var u *url.Error
	return errors.As(err, &u) || errors.Is(err, context.DeadlineExceeded)
}

type Provider struct {
	Config ProviderConfig
	HTTP   *http.Client
	tools  func() []mcp.Tool
}

func NewProvider(c ProviderConfig, tools func() []mcp.Tool, client *http.Client) (*Provider, error) {
	c.Name = strings.ToLower(strings.TrimSpace(c.Name))
	c.BaseURL = strings.TrimRight(c.BaseURL, "/")
	if c.ReasoningEffort == "" {
		c.ReasoningEffort = "none"
	}
	if c.Timeout <= 0 {
		c.Timeout = 60 * time.Second
	}
	if client == nil {
		client = &http.Client{Timeout: c.Timeout}
	} else if c.Name == "zai" || c.Name == "openrouter" {
		clone := *client
		clone.Timeout = c.Timeout
		client = &clone
	}
	p := &Provider{c, client, tools}
	switch c.Name {
	case "openai", "gemini", "deepseek", "groq", "openrouter", "zai":
	default:
		return nil, fmt.Errorf("unknown LLM provider %q", c.Name)
	}
	if strings.TrimSpace(c.APIKey) == "" {
		return nil, fmt.Errorf("%s API key must not be blank", c.Name)
	}
	if err := p.validateReasoning(); err != nil {
		return nil, err
	}
	diagnostics.Register(c.APIKey)
	slog.Info("Selected LLM", "provider", c.Name, "model", c.Model, "configured_effort", c.ReasoningEffort, "effective_effort", p.EffectiveReasoningEffort())
	return p, nil
}
func (p *Provider) SupportsImages() bool {
	m := strings.ToLower(strings.TrimSpace(p.Config.Model))
	switch p.Config.Name {
	case "openai", "gemini":
		return true
	case "deepseek":
		return m == "deepseek-flash" || m == "deepseek-v4-flash" || m == "deepseek-v4-flash-vision-exp"
	case "zai":
		return m == "glm-5.3-flash" || m == "glm-5.3-flashx"
	}
	return false
}
func (p *Provider) SupportsPDF() bool { return p.Config.Name == "zai" && p.SupportsImages() }
func DynamicContext(faq string, key int64, now time.Time) string {
	trusted := fmt.Sprintf("Текущая дата UTC: %s\nTelegram ID: %d", now.UTC().Format("2006-01-02"), key)
	if strings.TrimSpace(faq) != "" {
		return strings.TrimSpace(faq) + "\n\n" + trusted
	}
	return trusted
}
func (p *Provider) Initial(message string, key int64, faq, data, mime string, history []domain.Message) []Object {
	dynamic := DynamicContext(faq, key, time.Now())
	out := []Object{}
	if p.Config.Name == "gemini" {
		out = append(out, Object{"role": "user", "parts": []any{Object{"text": "[Система: Контекст текущего пользователя]\n" + dynamic}}}, Object{"role": "model", "parts": []any{Object{"text": "Принято. Я готов помочь пользователю."}}})
		for _, h := range history {
			role := "user"
			if h.Role == "assistant" {
				role = "model"
			}
			out = append(out, Object{"role": role, "parts": []any{Object{"text": h.Content}}})
		}
		parts := []any{Object{"text": message}}
		if strings.TrimSpace(data) != "" {
			if mime == "" {
				mime = "image/jpeg"
			}
			parts = append(parts, Object{"inline_data": Object{"mime_type": mime, "data": data}})
		}
		return append(out, Object{"role": "user", "parts": parts})
	}
	out = append(out, Object{"role": "system", "content": SystemPrompt}, Object{"role": "system", "content": dynamic})
	for _, h := range history {
		out = append(out, Object{"role": h.Role, "content": h.Content})
	}
	var content any = message
	if strings.TrimSpace(data) != "" {
		if mime == "" {
			mime = "image/jpeg"
		}
		uri := "data:" + mime + ";base64," + data
		parts := []any{}
		if strings.TrimSpace(message) != "" {
			typ := "text"
			if p.Config.Name == "openai" {
				typ = "input_text"
			}
			parts = append(parts, Object{"type": typ, "text": message})
		}
		switch {
		case p.Config.Name == "openai":
			parts = append(parts, Object{"type": "input_image", "image_url": uri})
		case mime == "application/pdf":
			parts = append(parts, Object{"type": "file", "file": Object{"file_data": uri}})
		default:
			parts = append(parts, Object{"type": "image_url", "image_url": Object{"url": uri}})
		}
		content = parts
	}
	return append(out, Object{"role": "user", "content": content})
}
func (p *Provider) toolDefinitions() []any {
	out := []any{}
	if p.tools == nil {
		return out
	}
	for _, t := range p.tools() {
		params := t.InputSchema
		if len(params) == 0 {
			params = Object{"type": "object", "properties": Object{}}
		}
		fn := Object{"name": t.Name, "description": t.Description, "parameters": params}
		switch p.Config.Name {
		case "openai":
			fn["type"] = "function"
			out = append(out, fn)
		case "gemini":
			decl := Object{"name": t.Name}
			if t.Description != "" {
				decl["description"] = t.Description
			}
			if len(t.InputSchema) > 0 {
				decl["parameters"] = SanitizeSchema(t.InputSchema)
			}
			out = append(out, decl)
		default:
			out = append(out, Object{"type": "function", "function": fn})
		}
	}
	return out
}
func (p *Provider) BuildRequest(conversation []Object) (Object, error) {
	tools := p.toolDefinitions()
	body := Object{"model": p.Config.Model, "messages": conversation}
	effort := p.Config.ReasoningEffort
	model := strings.ToLower(strings.TrimSpace(p.Config.Model))
	if len(tools) > 0 {
		body["tools"] = tools
		body["tool_choice"] = "auto"
	}
	switch p.Config.Name {
	case "openai":
		delete(body, "messages")
		body["input"] = conversation
		if len(openAIEfforts(model)) > 0 {
			body["reasoning"] = Object{"effort": effort}
		}
		if p.Config.Temperature != nil && (len(openAIEfforts(model)) == 0 || effort == "none") {
			body["temperature"] = *p.Config.Temperature
		}
	case "gemini":
		body = Object{"system_instruction": Object{"parts": []any{Object{"text": SystemPrompt}}}, "contents": conversation, "tool_config": Object{"function_calling_config": Object{"mode": "AUTO"}}}
		if len(tools) > 0 {
			body["tools"] = []any{Object{"function_declarations": tools}}
		}
		thinking, err := geminiThinking(model, effort)
		if err != nil {
			return nil, err
		}
		if thinking != nil {
			body["generationConfig"] = Object{"thinkingConfig": thinking}
		}
	case "deepseek":
		supported := deepseekReasoning(model)
		enabled := supported && effort != "none"
		if supported {
			typ := "disabled"
			if enabled {
				typ = "enabled"
				body["reasoning_effort"] = deepseekEffort(effort)
			}
			body["thinking"] = Object{"type": typ}
		}
		if enabled {
			delete(body, "tool_choice")
		} else {
			body["temperature"] = 0.3
		}
	case "groq":
		body["temperature"] = 0.3
		for k, v := range groqParameters(model, effort) {
			body[k] = v
		}
	case "openrouter":
		body["stream"] = false
		switch model {
		case "z-ai/glm-4.7", "z-ai/glm-5.3-flash":
			body["reasoning"] = Object{"enabled": effort != "none"}
		case "z-ai/glm-5.3":
			body["reasoning"] = Object{"effort": glmEffort(effort)}
		}
	case "zai":
		body["stream"] = false
		switch model {
		case "glm-4.7":
			typ := "disabled"
			if effort != "none" {
				typ = "enabled"
			}
			body["thinking"] = Object{"type": typ}
		case "glm-5.3", "glm-5.3-flash", "glm-5.3-flashx":
			thinking := Object{"type": "enabled"}
			if p.SupportsImages() {
				thinking["clear_thinking"] = false
			}
			body["thinking"] = thinking
			body["reasoning_effort"] = glmEffort(effort)
		}
	}
	return body, nil
}
func (p *Provider) Call(ctx context.Context, conversation []Object) (Object, error) {
	ctx = diagnostics.WithRequest(ctx, diagnostics.NewID())
	ctx = diagnostics.WithOperation(ctx, "llm", "call_api")
	body, err := p.BuildRequest(conversation)
	if err != nil {
		return nil, err
	}
	endpoint := p.Config.BaseURL + "/chat/completions"
	if p.Config.Name == "openai" {
		endpoint = p.Config.BaseURL + "/responses"
	}
	if p.Config.Name == "gemini" {
		endpoint = p.Config.BaseURL + "/models/" + url.PathEscape(p.Config.Model) + ":generateContent"
	}
	encoded, err := json.Marshal(body)
	if err != nil {
		return nil, parseError("request JSON encoding failed")
	}
	for attempt := 0; attempt < 3; attempt++ {
		started := time.Now()
		requestCtx, cancel := context.WithTimeout(ctx, p.Config.Timeout)
		req, err := http.NewRequestWithContext(requestCtx, "POST", endpoint, bytes.NewReader(encoded))
		if err != nil {
			cancel()
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		if p.Config.Name == "gemini" {
			req.Header.Set("x-goog-api-key", p.Config.APIKey)
		} else {
			req.Header.Set("Authorization", "Bearer "+p.Config.APIKey)
		}
		diagnostics.Payload(ctx, "LLM HTTP request", Object{"url": req.URL.String(), "headers": req.Header, "body": body}, "provider", p.Config.Name, "model", p.Config.Model, "attempt", attempt+1)
		res, err := p.HTTP.Do(req)
		var raw []byte
		if err == nil {
			raw, err = io.ReadAll(io.LimitReader(res.Body, 32<<20))
			res.Body.Close()
		}
		cancel()
		status := 0
		if res != nil {
			status = res.StatusCode
		}
		duration := time.Since(started).Seconds()
		if err == nil {
			diagnostics.Info(ctx, "LLM HTTP attempt", "provider", p.Config.Name, "model", p.Config.Model, "attempt", attempt+1, "status_code", status, "duration_seconds", duration)
			diagnostics.Payload(ctx, "LLM HTTP response", raw, "provider", p.Config.Name, "model", p.Config.Model, "attempt", attempt+1, "status_code", status, "duration_seconds", duration)
		} else {
			diagnostics.Error(ctx, "LLM HTTP request failed", err, "provider", p.Config.Name, "model", p.Config.Model, "attempt", attempt+1, "duration_seconds", duration)
		}
		retryable := err != nil
		if err == nil {
			switch res.StatusCode {
			case 408, 429, 500, 502, 503, 504:
				retryable = true
			}
		}
		if retryable && attempt < 2 && ctx.Err() == nil {
			delay := time.Duration(float64(500*time.Millisecond) * float64(int64(1)<<uint(attempt)) * (0.5 + rand.Float64()/2))
			if res != nil {
				if seconds, e := strconv.ParseFloat(strings.TrimSpace(res.Header.Get("Retry-After")), 64); e == nil && seconds > 0 {
					if seconds > 8 {
						seconds = 8
					}
					delay = time.Duration(seconds * float64(time.Second))
				}
			}
			diagnostics.Info(ctx, "LLM HTTP retry", "provider", p.Config.Name, "model", p.Config.Model, "attempt", attempt+2, "delay_seconds", delay.Seconds(), "status_code", status)
			timer := time.NewTimer(delay)
			select {
			case <-ctx.Done():
				timer.Stop()
				return nil, ctx.Err()
			case <-timer.C:
			}
			continue
		}
		if err != nil {
			return nil, &ProcessingError{Message: "Provider transport request failed", Friendly: defaultUserError, Fallback: ctx.Err() == nil, Cause: err}
		}
		var payload Object
		decodeErr := json.Unmarshal(raw, &payload)
		if e := p.checkError(res.StatusCode, payload, raw); e != nil {
			diagnostics.Error(ctx, "LLM provider API error", e, "provider", p.Config.Name, "model", p.Config.Model, "status_code", res.StatusCode)
			return nil, e
		}
		if decodeErr != nil || payload == nil {
			diagnostics.Error(ctx, "LLM provider JSON decode failed", decodeErr, "provider", p.Config.Name, "model", p.Config.Model, "status_code", res.StatusCode)
			return nil, parseError("Provider returned invalid JSON object")
		}
		return payload, nil
	}
	return nil, processing("Provider exhausted retries")
}
func (p *Provider) checkError(status int, payload Object, raw []byte) error {
	e, exists := payload["error"]
	if p.Config.Name == "openrouter" && !exists {
		choices := arr(payload["choices"])
		if len(choices) > 0 {
			e, exists = obj(choices[0])["error"]
		}
	}
	var code any
	message := ""
	if em, ok := e.(map[string]any); ok {
		code = em["code"]
		message = str(em["message"])
	} else if e != nil {
		message = fmt.Sprint(e)
	}
	if p.Config.Name == "zai" && !exists {
		if c, ok := payload["code"]; ok && c != nil && fmt.Sprint(c) != "0" && fmt.Sprint(c) != "200" {
			exists = true
			code = c
			message = str(payload["msg"])
			if message == "" {
				message = str(payload["message"])
			}
		}
	}
	exhausted := balance(message) || balance(string(raw)) || (p.Config.Name == "zai" && fmt.Sprint(code) == "1113")
	if status >= 400 {
		return &ProcessingError{Message: fmt.Sprintf("%s API error (model=%s, status=%d)", p.Config.Name, p.Config.Model, status), Friendly: defaultUserError, Status: status, Fallback: exhausted}
	}
	if p.Config.Name != "zai" && p.Config.Name != "openrouter" {
		return nil
	}
	if exists && (e != nil || p.Config.Name == "openrouter" || code != nil) {
		if p.Config.Name == "openrouter" {
			if _, ok := e.(map[string]any); !ok {
				return parseError("OpenRouter invalid error envelope")
			}
		}
		n := 0
		if p.Config.Name == "openrouter" {
			n, _ = strconv.Atoi(fmt.Sprint(code))
			if n < 100 || n > 599 {
				n = 0
			}
		}
		return &ProcessingError{Message: fmt.Sprintf("%s API business error (model=%s)", p.Config.Name, p.Config.Model), Friendly: defaultUserError, Status: n, Fallback: exhausted}
	}
	return nil
}
func obj(v any) Object { m, _ := v.(map[string]any); return m }
func arr(v any) []any  { a, _ := v.([]any); return a }
func str(v any) string { s, _ := v.(string); return s }
func number(v any) int64 {
	switch x := v.(type) {
	case float64:
		return int64(x)
	case int64:
		return x
	case json.Number:
		n, _ := x.Int64()
		return n
	}
	return 0
}
func jsonText(v any) string { b, _ := json.Marshal(v); return string(b) }

func friendlyError(s, friendly string) *ProcessingError {
	return &ProcessingError{Message: s, Friendly: friendly}
}
