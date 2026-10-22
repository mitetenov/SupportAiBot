package llm

import (
	"fmt"
	"strings"
)

func effortSet(s string) map[string]bool {
	m := map[string]bool{}
	for _, v := range strings.Fields(s) {
		m[v] = true
	}
	return m
}
func openAIEfforts(model string) map[string]bool {
	switch {
	case model == "gpt-6-astra":
		return effortSet("low medium high xhigh max")
	case model == "gpt-6-sol" || model == "gpt-6-luna" || strings.HasPrefix(model, "gpt-5.6"):
		return effortSet("none low medium high xhigh max")
	case strings.HasPrefix(model, "gpt-5.5-pro"):
		return effortSet("medium high xhigh")
	case strings.HasPrefix(model, "gpt-5.5"):
		return effortSet("none low medium high xhigh")
	case strings.HasPrefix(model, "gpt-5-pro"):
		return effortSet("high")
	case model == "gpt-5" || strings.HasPrefix(model, "gpt-5-"):
		return effortSet("minimal low medium high")
	case strings.HasPrefix(model, "gpt-5.1") || strings.HasPrefix(model, "gpt-5.2") || strings.HasPrefix(model, "gpt-5.3") || strings.HasPrefix(model, "gpt-5.4"):
		return effortSet("none low medium high xhigh")
	case strings.HasPrefix(model, "o1") || strings.HasPrefix(model, "o3") || strings.HasPrefix(model, "o4"):
		return effortSet("low medium high")
	}
	return nil
}
func deepseekReasoning(m string) bool {
	return strings.HasPrefix(m, "deepseek-v4-") || m == "deepseek-flash" || m == "deepseek-chat" || m == "deepseek-reasoner"
}
func deepseekEffort(e string) string {
	switch e {
	case "minimal", "low":
		return "low"
	case "max":
		return "max"
	default:
		return "high"
	}
}
func glmEffort(e string) string {
	switch e {
	case "none", "minimal", "low":
		return "low"
	case "medium", "high":
		return "high"
	case "xhigh", "max":
		return "max"
	}
	return "none"
}
func groqParameters(m, e string) Object {
	native := e
	switch e {
	case "none", "minimal":
		native = "low"
	case "xhigh", "max":
		native = "high"
	}
	switch m {
	case "openai/gpt-oss-20b", "openai/gpt-oss-120b", "openai/gpt-oss-safeguard-20b":
		return Object{"reasoning_effort": native, "include_reasoning": false}
	case "qwen/qwen3-32b", "qwen/qwen3.6-27b":
		if e == "none" {
			native = "none"
		} else {
			native = "default"
		}
		return Object{"reasoning_effort": native, "reasoning_format": "hidden"}
	case "qwen/qwen3.8-27b":
		if e == "none" {
			native = "none"
		}
		return Object{"reasoning_effort": native, "reasoning_format": "hidden"}
	}
	return nil
}
func geminiThinking(m, e string) (Object, error) {
	if strings.HasPrefix(m, "gemini-3") {
		var allowed map[string]bool
		switch {
		case strings.Contains(m, "3.1-flash-lite-image"):
			allowed = effortSet("minimal high")
		case strings.Contains(m, "3.7-flash") || strings.Contains(m, "3.1-pro"):
			allowed = effortSet("low medium high")
		case strings.Contains(m, "3-pro"):
			allowed = effortSet("low high")
		case strings.Contains(m, "3.6-flash") || strings.Contains(m, "3.5-flash") || strings.Contains(m, "3.1-flash-lite") || strings.Contains(m, "3-flash"):
			allowed = effortSet("minimal low medium high")
		default:
			return nil, fmt.Errorf("unknown Gemini thinking contract for %s", m)
		}
		native := e
		if e == "none" {
			native = "low"
			if allowed["minimal"] {
				native = "minimal"
			}
		}
		if e == "xhigh" || e == "max" {
			native = "high"
		}
		if !allowed[native] {
			return nil, fmt.Errorf("gemini model %s does not support reasoning effort %s", m, e)
		}
		return Object{"thinkingLevel": native}, nil
	}
	if strings.HasPrefix(m, "gemini-2.5") {
		budget := map[string]int{"none": 0, "minimal": 512, "low": 1024, "medium": 8192, "high": 24576, "xhigh": 24576, "max": 24576}
		if strings.Contains(m, "pro") {
			if e == "none" {
				return nil, nil
			}
			budget["minimal"] = 128
			budget["xhigh"] = 32768
			budget["max"] = 32768
		}
		return Object{"thinkingBudget": budget[e]}, nil
	}
	return nil, nil
}
func (p *Provider) validateReasoning() error {
	m := strings.ToLower(strings.TrimSpace(p.Config.Model))
	e := p.Config.ReasoningEffort
	if !effortSet("none minimal low medium high xhigh max")[e] {
		return fmt.Errorf("invalid reasoning effort %q", e)
	}
	switch p.Config.Name {
	case "openai":
		if a := openAIEfforts(m); a != nil && !a[e] {
			return fmt.Errorf("OpenAI model %s does not support reasoning effort %s", m, e)
		}
	case "gemini":
		_, err := geminiThinking(m, e)
		return err
	case "zai":
		if m == "glm-5.3" && e == "none" {
			return fmt.Errorf("Z.AI glm-5.3 requires reasoning")
		}
	case "openrouter":
		if m == "z-ai/glm-5.3" && e == "none" {
			return fmt.Errorf("OpenRouter z-ai/glm-5.3 requires reasoning")
		}
	}
	return nil
}
func SanitizeSchema(schema Object) Object {
	clean := Object{}
	for k, v := range schema {
		switch k {
		case "$schema", "additionalProperties", "propertyNames":
			continue
		case "const":
			clean["enum"] = []any{v}
		case "properties":
			props := obj(v)
			if props == nil {
				clean[k] = v
				continue
			}
			out := Object{}
			for name, raw := range props {
				if nested := obj(raw); nested != nil {
					out[name] = SanitizeSchema(nested)
				} else {
					out[name] = raw
				}
			}
			clean[k] = out
		case "items":
			if nested := obj(v); nested != nil {
				clean[k] = SanitizeSchema(nested)
			} else {
				clean[k] = v
			}
		case "any_of", "anyOf", "oneOf", "allOf":
			target := k
			if k == "any_of" {
				target = "anyOf"
			}
			if list, ok := v.([]any); ok {
				out := []any{}
				for _, raw := range list {
					if nested := obj(raw); nested != nil {
						out = append(out, SanitizeSchema(nested))
					} else {
						out = append(out, raw)
					}
				}
				clean[target] = out
			} else {
				clean[target] = v
			}
		default:
			clean[k] = v
		}
	}
	return clean
}

// EffectiveReasoningEffort reports the native mode actually sent over the wire.
func (p *Provider) EffectiveReasoningEffort() string {
	model := strings.ToLower(strings.TrimSpace(p.Config.Model))
	effort := p.Config.ReasoningEffort
	switch p.Config.Name {
	case "openai":
		if len(openAIEfforts(model)) > 0 {
			return effort
		}
	case "gemini":
		thinking, err := geminiThinking(model, effort)
		if err == nil && thinking != nil {
			if level, ok := thinking["thinkingLevel"].(string); ok {
				return level
			}
			if budget, ok := thinking["thinkingBudget"]; ok {
				return fmt.Sprintf("budget=%v", budget)
			}
		}
	case "deepseek":
		if deepseekReasoning(model) {
			if effort == "none" {
				return "none"
			}
			return deepseekEffort(effort)
		}
	case "groq":
		if params := groqParameters(model, effort); params != nil {
			return str(params["reasoning_effort"])
		}
	case "zai":
		if model == "glm-4.7" {
			if effort == "none" {
				return "none"
			}
			return "enabled"
		}
		if model == "glm-5.3" || model == "glm-5.3-flash" || model == "glm-5.3-flashx" {
			return glmEffort(effort)
		}
	case "openrouter":
		if model == "z-ai/glm-4.7" || model == "z-ai/glm-5.3-flash" {
			if effort == "none" {
				return "none"
			}
			return "enabled"
		}
		if model == "z-ai/glm-5.3" {
			return glmEffort(effort)
		}
	}
	return "unsupported/ignored"
}
