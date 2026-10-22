package llm

import (
	"encoding/json"
	"regexp"
	"strings"
	"supportbot/internal/domain"
)

var rawReasoning = regexp.MustCompile(`(?is)<think\b[^>]*>.*?(?:</think\s*>|$)`)

func (p *Provider) Parse(payload Object) (Response, error) {
	switch p.Config.Name {
	case "openai":
		return p.parseOpenAI(payload)
	case "gemini":
		return p.parseGemini(payload)
	default:
		return p.parseChat(payload)
	}
}
func (p *Provider) parseOpenAI(payload Object) (Response, error) {
	output := arr(payload["output"])
	if len(output) == 0 {
		return Response{}, friendlyError("Empty output", "Не удалось получить ответ от модели. Попробуйте позже.")
	}
	r := Response{Raw: output}
	for _, raw := range output {
		item := obj(raw)
		switch item["type"] {
		case "function_call":
			a := item["arguments"]
			args := obj(a)
			if s, ok := a.(string); ok {
				json.Unmarshal([]byte(s), &args)
			}
			if args == nil {
				args = Object{}
			}
			r.Calls = append(r.Calls, ToolCall{Name: str(item["name"]), ID: str(item["call_id"]), Arguments: args})
		case "message":
			for _, part := range arr(item["content"]) {
				p := obj(part)
				if p["type"] == "output_text" {
					r.Text += str(p["text"])
				}
			}
		}
	}
	if r.Text == "" && len(r.Calls) == 0 {
		return Response{}, friendlyError("Empty response", "Модель не вернула ответа. Попробуйте переформулировать вопрос.")
	}
	return r, nil
}
func (p *Provider) parseGemini(payload Object) (Response, error) {
	candidates := arr(payload["candidates"])
	if len(candidates) == 0 {
		return Response{}, friendlyError("Empty candidates", "Не удалось получить ответ от модели. Возможно, запрос был заблокирован фильтрами.")
	}
	parts := arr(obj(obj(candidates[0])["content"])["parts"])
	if len(parts) == 0 {
		return Response{}, friendlyError("Empty parts", "Модель не вернула ответа. Попробуйте переформулировать вопрос.")
	}
	r := Response{Raw: parts}
	for _, raw := range parts {
		part := obj(raw)
		r.Text += str(part["text"])
		if fc := obj(part["functionCall"]); fc != nil {
			args := obj(fc["args"])
			if args == nil {
				args = Object{}
			}
			sig := part["thoughtSignature"]
			if sig == nil {
				sig = part["thought_signature"]
			}
			if sig == nil {
				sig = fc["thoughtSignature"]
			}
			if sig == nil {
				sig = fc["thought_signature"]
			}
			r.Calls = append(r.Calls, ToolCall{Name: str(fc["name"]), ID: str(fc["id"]), Arguments: args, ThoughtSignature: sig})
		}
	}
	return r, nil
}
func (p *Provider) parseChat(payload Object) (Response, error) {
	choices := arr(payload["choices"])
	if len(choices) == 0 {
		return Response{}, friendlyError("Empty choices", "Не удалось получить ответ от модели. Попробуйте позже.")
	}
	first := obj(choices[0])
	message := obj(first["message"])
	if len(message) == 0 {
		return Response{}, friendlyError("No message in response", "Не удалось получить ответ от модели. Попробуйте позже.")
	}
	strict := p.Config.Name == "zai" || p.Config.Name == "openrouter"
	calls := arr(message["tool_calls"])
	if strict {
		if finish, exists := first["finish_reason"]; exists {
			if finish == "network_error" {
				e := processing("Provider network error")
				e.Fallback = p.Config.Name == "zai"
				return Response{}, e
			}
			if (finish != "stop" || len(calls) != 0) && (finish != "tool_calls" || len(calls) <= 0) {
				return Response{}, friendlyError("Invalid finish_reason", "Не удалось получить завершённый ответ от модели. Попробуйте позже.")
			}
		}
	}
	r := Response{}
	if raw, exists := message["content"]; exists && raw != nil {
		content, ok := raw.(string)
		if !ok {
			return Response{}, parseError("Message content must be string or null")
		}
		r.Text = content
	}
	if p.Config.Name != "deepseek" {
		r.Text = strings.TrimSpace(rawReasoning.ReplaceAllString(r.Text, ""))
	}
	if raw, exists := message["tool_calls"]; strict && exists && raw != nil && calls == nil {
		return Response{}, parseError("tool_calls must be a list")
	}
	seen := map[string]bool{}
	for _, raw := range calls {
		tc := obj(raw)
		fn := obj(tc["function"])
		id := str(tc["id"])
		name := str(fn["name"])
		a, hasArgs := fn["arguments"]
		args := obj(a)
		var decodeErr error
		if s, ok := a.(string); ok {
			decodeErr = json.Unmarshal([]byte(s), &args)
		}
		if strict {
			if tc == nil || fn == nil || strings.TrimSpace(id) == "" || seen[id] || strings.TrimSpace(name) == "" || !hasArgs || a == nil || decodeErr != nil || args == nil {
				return Response{}, parseError("Malformed tool call")
			}
		}
		seen[id] = true
		if args == nil {
			args = Object{}
		}
		r.Calls = append(r.Calls, ToolCall{Name: name, ID: id, Arguments: args})
	}
	reasoning := message["reasoning_content"]
	if p.Config.Name == "groq" {
		reasoning = message["reasoning"]
	}
	if p.Config.Name == "groq" || p.Config.Name == "openrouter" {
		if s, ok := message["reasoning"].(string); ok {
			reasoning = s
		}
	}
	if s, ok := reasoning.(string); ok {
		r.Reasoning = &s
	}
	if p.Config.Name == "openrouter" {
		for _, raw := range arr(message["reasoning_details"]) {
			if obj(raw) != nil {
				r.ReasoningDetails = append(r.ReasoningDetails, raw)
			}
		}
	}
	return r, nil
}
func (p *Provider) AppendCalls(c []Object, r Response) []Object {
	switch p.Config.Name {
	case "openai":
		if len(r.Raw) > 0 {
			for _, raw := range r.Raw {
				if m := obj(raw); m != nil {
					c = append(c, m)
				}
			}
			return c
		}
		for _, tc := range r.Calls {
			c = append(c, Object{"type": "function_call", "call_id": tc.ID, "name": tc.Name, "arguments": jsonText(tc.Arguments)})
		}
		return c
	case "gemini":
		parts := r.Raw
		if len(parts) == 0 {
			if r.Text != "" {
				parts = append(parts, Object{"text": r.Text})
			}
			for _, tc := range r.Calls {
				fc := Object{"name": tc.Name, "args": tc.Arguments}
				if tc.ID != "" {
					fc["id"] = tc.ID
				}
				part := Object{"functionCall": fc}
				if tc.ThoughtSignature != nil {
					part["thoughtSignature"] = tc.ThoughtSignature
				}
				parts = append(parts, part)
			}
		}
		return append(c, Object{"role": "model", "parts": parts})
	}
	calls := []any{}
	for _, tc := range r.Calls {
		calls = append(calls, Object{"id": tc.ID, "type": "function", "function": Object{"name": tc.Name, "arguments": jsonText(tc.Arguments)}})
	}
	var content any = r.Text
	if r.Text == "" && p.Config.Name != "deepseek" && p.Config.Name != "groq" {
		content = nil
	}
	assistant := Object{"role": "assistant", "content": content, "tool_calls": calls}
	if r.Reasoning != nil {
		field := "reasoning_content"
		if p.Config.Name == "openrouter" {
			field = "reasoning"
		}
		if p.Config.Name != "groq" {
			assistant[field] = *r.Reasoning
		}
	}
	if len(r.ReasoningDetails) > 0 {
		assistant["reasoning_details"] = r.ReasoningDetails
	}
	return append(c, assistant)
}
func (p *Provider) AppendResult(c []Object, call ToolCall, result string) []Object {
	switch p.Config.Name {
	case "openai":
		return append(c, Object{"type": "function_call_output", "call_id": call.ID, "output": result})
	case "gemini":
		var value any
		if json.Unmarshal([]byte(result), &value) != nil {
			value = Object{"output": result}
		}
		response := Object{"name": call.Name, "response": value}
		if call.ID != "" {
			response["id"] = call.ID
		}
		return append(c, Object{"role": "user", "parts": []any{Object{"functionResponse": response}}})
	default:
		return append(c, Object{"role": "tool", "tool_call_id": call.ID, "content": result})
	}
}
func (p *Provider) Nudge(c []Object, text, instruction string) []Object {
	if p.Config.Name == "gemini" {
		if text != "" {
			c = append(c, Object{"role": "model", "parts": []any{Object{"text": text}}})
		}
		return append(c, Object{"role": "user", "parts": []any{Object{"text": instruction}}})
	}
	if text != "" {
		c = append(c, Object{"role": "assistant", "content": text})
	}
	return append(c, Object{"role": "user", "content": instruction})
}
func (p *Provider) Usage(payload Object) (domain.TokenUsage, bool) {
	field := "usage"
	if p.Config.Name == "gemini" {
		field = "usageMetadata"
	}
	u := obj(payload[field])
	if u == nil {
		return domain.TokenUsage{}, false
	}
	a, b, total := "prompt_tokens", "completion_tokens", "total_tokens"
	if p.Config.Name == "gemini" {
		a, b, total = "promptTokenCount", "candidatesTokenCount", "totalTokenCount"
	}
	if p.Config.Name == "openai" {
		a, b = "input_tokens", "output_tokens"
	}
	r := domain.TokenUsage{PromptTokens: number(u[a]), CompletionTokens: number(u[b]), TotalTokens: number(u[total])}
	if p.Config.Name == "openai" {
		if r.PromptTokens == 0 {
			r.PromptTokens = number(u["prompt_tokens"])
		}
		if r.CompletionTokens == 0 {
			r.CompletionTokens = number(u["completion_tokens"])
		}
	}
	if r.TotalTokens == 0 && p.Config.Name != "gemini" {
		r.TotalTokens = r.PromptTokens + r.CompletionTokens
	}
	return r, true
}
