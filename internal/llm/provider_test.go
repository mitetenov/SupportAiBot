package llm

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"reflect"
	"regexp"
	"strings"
	"supportbot/internal/domain"
	"supportbot/internal/mcp"
	"testing"
	"time"
)

func fixtureTools() []mcp.Tool {
	return []mcp.Tool{{Name: "example", Description: "Test", InputSchema: Object{"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "properties": Object{"telegram_id": Object{"type": "integer"}, "filter": Object{"const": "value"}}, "additionalProperties": false, "required": []any{"telegram_id"}}}}
}
func TestPythonRequestSnapshots(t *testing.T) {
	raw, err := os.ReadFile("testdata/python_requests.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Provider, Model, Effort string
		Body                    Object
		Error, Images, PDF      bool
	}
	if err = json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	for _, tc := range cases {
		t.Run(tc.Provider+"/"+tc.Model+"/"+tc.Effort, func(t *testing.T) {
			p, e := NewProvider(ProviderConfig{Name: tc.Provider, Model: tc.Model, APIKey: "sk-test", ReasoningEffort: tc.Effort}, fixtureTools, nil)
			if tc.Error {
				if e == nil {
					t.Fatal("Python rejects configuration but Go accepts it")
				}
				return
			}
			if e != nil {
				t.Fatal(e)
			}
			// Retain the Python wire fixture, with the explicitly revised Go
			// business-grounding prompt. All other request fields stay exact.
			if tc.Provider == "gemini" {
				obj(arr(obj(tc.Body["system_instruction"])["parts"])[0])["text"] = SystemPrompt
			}
			if p.SupportsImages() != tc.Images || p.SupportsPDF() != tc.PDF {
				t.Fatalf("capability differs: image=%v pdf=%v", p.SupportsImages(), p.SupportsPDF())
			}
			body, e := p.BuildRequest([]Object{{"role": "user", "content": "request"}})
			if e != nil {
				t.Fatal(e)
			}
			encoded, _ := json.Marshal(body)
			var normalized Object
			json.Unmarshal(encoded, &normalized)
			if !reflect.DeepEqual(normalized, tc.Body) {
				t.Errorf("request differs from Python\ngot: %s\nwant: %s", encoded, jsonText(tc.Body))
			}
		})
	}
}
func TestProviderMediaAndRawReplay(t *testing.T) {
	for _, tc := range []struct {
		provider, model string
		images, pdf     bool
	}{{"openai", "gpt-6-luna", true, false}, {"gemini", "gemini-3.5-flash-lite", true, false}, {"deepseek", "deepseek-flash", true, false}, {"groq", "llama", false, false}, {"openrouter", "z-ai/glm-5.3-flash", false, false}, {"zai", "glm-5.3-flash", true, true}, {"zai", "glm-5.3-flashx", true, true}} {
		t.Run(tc.provider+tc.model, func(t *testing.T) {
			p, e := NewProvider(ProviderConfig{Name: tc.provider, Model: tc.model, APIKey: "test"}, fixtureTools, nil)
			if e != nil {
				t.Fatal(e)
			}
			if p.SupportsImages() != tc.images || p.SupportsPDF() != tc.pdf {
				t.Fatal("wrong capabilities")
			}
			if tc.images {
				c := p.Initial("", 42, "FAQ", "YWJj", "image/png", nil)
				if !strings.Contains(jsonText(c), "YWJj") {
					t.Fatal("image data missing")
				}
			}
			if tc.pdf {
				c := p.Initial("PDF", 42, "FAQ", "YWJj", "application/pdf", nil)
				if !strings.Contains(jsonText(c), `"file_data":"data:application/pdf;base64,YWJj"`) {
					t.Fatal(jsonText(c))
				}
			}
		})
	}
	p, _ := NewProvider(ProviderConfig{Name: "gemini", Model: "gemini-3.5-flash-lite", APIKey: "test"}, nil, nil)
	raw := Object{"candidates": []any{Object{"content": Object{"parts": []any{Object{"thought": true, "text": "private", "thoughtSignature": "opaque"}, Object{"functionCall": Object{"id": "f1", "name": "example", "args": Object{"x": 1}}, "thoughtSignature": "call-signature"}}}}}}
	r, e := p.Parse(raw)
	if e != nil {
		t.Fatal(e)
	}
	c := p.AppendCalls(nil, r)
	if !reflect.DeepEqual(c[0]["parts"], obj(obj(arr(raw["candidates"])[0])["content"])["parts"]) {
		t.Fatal("Gemini parts/signatures changed")
	}
	c = p.AppendResult(c, r.Calls[0], `{"ok":true}`)
	if !strings.Contains(jsonText(c), `"id":"f1"`) {
		t.Fatal("call ID omitted")
	}
	p, _ = NewProvider(ProviderConfig{Name: "openai", Model: "gpt-6-luna", APIKey: "test"}, nil, nil)
	items := []any{Object{"type": "reasoning", "id": "opaque", "encrypted_content": "secret-state"}, Object{"type": "function_call", "name": "example", "call_id": "call1", "arguments": "{}"}}
	r, e = p.Parse(Object{"output": items})
	if e != nil {
		t.Fatal(e)
	}
	c = p.AppendCalls(nil, r)
	encoded, _ := json.Marshal(c)
	if string(encoded) != jsonText(items) {
		t.Fatal("OpenAI raw output lost")
	}
}
func TestTerminalErrorAndReasoning(t *testing.T) {
	for _, provider := range []string{"zai", "openrouter"} {
		p, _ := NewProvider(ProviderConfig{Name: provider, Model: "custom", APIKey: "test"}, nil, nil)
		for _, reason := range []any{nil, "length", "content_filter", "network_error", "stop"} {
			payload := Object{"choices": []any{Object{"finish_reason": reason, "message": Object{"content": "partial"}}}}
			_, err := p.Parse(payload)
			if reason == "stop" {
				if err != nil {
					t.Fatal(err)
				}
			} else if err == nil {
				t.Fatalf("accepted partial terminal %v", reason)
			}
		}
		r, e := p.Parse(Object{"choices": []any{Object{"message": Object{"content": "<think>hidden</think>Answer", "reasoning_content": "retain", "tool_calls": []any{Object{"id": "c", "function": Object{"name": "example", "arguments": "{}"}}}}}}})
		if e != nil {
			t.Fatal(e)
		}
		if r.Text != "Answer" {
			t.Fatalf("reasoning leaked %q", r.Text)
		}
		c := p.AppendCalls(nil, r)
		if !strings.Contains(jsonText(c), "retain") {
			t.Fatal("reasoning state lost")
		}
		if _, e = p.Parse(Object{"choices": []any{Object{"message": Object{"tool_calls": []any{Object{"id": "x", "function": Object{"name": "example", "arguments": "[]"}}}}}}}); e == nil {
			t.Fatal("accepted non-object tool args")
		}
	}
}
func TestPromptTrustedIdentityAndPolicies(t *testing.T) {
	s := DynamicContext("Telegram ID: 999", 42, time.Date(2026, 10, 20, 0, 0, 0, 0, time.UTC))
	if !strings.HasSuffix(s, "Текущая дата UTC: 2026-10-20\nTelegram ID: 42") {
		t.Fatal(s)
	}
	for _, s := range []string{"не помогло", "Всё ещё не работает", "то же самое", "другое!"} {
		if !IsRejection(s) {
			t.Fatal(s)
		}
	}
	for _, s := range []string{"не только", "подключить другое устройство"} {
		if IsRejection(s) {
			t.Fatal(s)
		}
	}
	if UserRequestsHuman("живу в Германии") || !UserRequestsHuman("нужен живой человек") {
		t.Fatal("human morphology failed")
	}
}
func TestPythonResponseSnapshots(t *testing.T) {
	raw, err := os.ReadFile("testdata/python_responses.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Provider, Model   string
		Payload           Object
		Error, Fallback   bool
		Text, UserMessage string `json:"-"`
		Reasoning         *string
		Calls             []struct {
			Name, ID         string
			Arguments        any
			ThoughtSignature any
		}
		Replay []Object
	}
	var generic []Object
	json.Unmarshal(raw, &generic)
	if err = json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	for i, tc := range cases {
		t.Run(tc.Provider+"/"+fmt.Sprint(i), func(t *testing.T) {
			p, e := NewProvider(ProviderConfig{Name: tc.Provider, Model: tc.Model, APIKey: "test", ReasoningEffort: "high"}, nil, nil)
			if e != nil {
				t.Fatal(e)
			}
			response, e := p.Parse(tc.Payload)
			if tc.Error {
				if e == nil {
					t.Fatal("Python rejected payload but Go accepted it")
				}
				var pe *ProcessingError
				if !errors.As(e, &pe) {
					t.Fatal(e)
				}
				want := str(generic[i]["user_message"])
				if pe.UserMessage() != want || pe.Fallback != tc.Fallback {
					t.Fatalf("error contract differs got=%q fallback=%v want=%q fallback=%v", pe.UserMessage(), pe.Fallback, want, tc.Fallback)
				}
				return
			}
			if e != nil {
				t.Fatal(e)
			}
			wantText := str(generic[i]["text"])
			if response.Text != wantText {
				t.Fatalf("text differs got %q want %q", response.Text, wantText)
			}
			if !reflect.DeepEqual(response.Reasoning, tc.Reasoning) {
				t.Fatal("reasoning differs")
			}
			actual := p.AppendCalls(nil, response)
			a, _ := json.Marshal(actual)
			b, _ := json.Marshal(tc.Replay)
			var an, bn any
			json.Unmarshal(a, &an)
			json.Unmarshal(b, &bn)
			canonicalArguments(an)
			canonicalArguments(bn)
			if !reflect.DeepEqual(an, bn) {
				t.Fatalf("replay differs got %s want %s", a, b)
			}
		})
	}
}

func canonicalArguments(v any) {
	switch x := v.(type) {
	case []any:
		for _, item := range x {
			canonicalArguments(item)
		}
	case map[string]any:
		for k, item := range x {
			if k == "arguments" {
				if s, ok := item.(string); ok {
					var parsed any
					if json.Unmarshal([]byte(s), &parsed) == nil {
						x[k] = parsed
					}
				}
			}
			canonicalArguments(item)
		}
	}
}
func TestPythonInitialConversationSnapshots(t *testing.T) {
	raw, err := os.ReadFile("testdata/python_conversations.json")
	if err != nil {
		t.Fatal(err)
	}
	var rows []struct {
		Provider, Model, Kind, Mime string
		Conversation                []Object
	}
	if json.Unmarshal(raw, &rows) != nil {
		t.Fatal("invalid fixtures")
	}
	for _, row := range rows {
		t.Run(row.Provider+row.Kind, func(t *testing.T) {
			// The incident fix deliberately changes policy text, not the
			// Python conversation shape, history, media or trusted identity.
			if row.Provider != "gemini" {
				row.Conversation[0]["content"] = SystemPrompt
			}
			p, e := NewProvider(ProviderConfig{Name: row.Provider, Model: row.Model, ReasoningEffort: "high", APIKey: "test"}, nil, nil)
			if e != nil {
				t.Fatal(e)
			}
			data := ""
			if row.Kind != "text" {
				data = "YWJj"
			}
			c := p.Initial("Помоги проверить", 42, "  FAQ evidence  ", data, row.Mime, []domain.Message{{Role: "user", Content: "Моя подписка"}, {Role: "assistant", Content: "Уточните приложение"}})
			current := jsonText(c)
			current = dateContext.ReplaceAllString(current, "Текущая дата UTC: DATE")
			var actual []Object
			json.Unmarshal([]byte(current), &actual)
			if !reflect.DeepEqual(actual, row.Conversation) {
				t.Fatalf("initial wire context differs\ngot %s\nwant %s", current, jsonText(row.Conversation))
			}
		})
	}
}

var dateContext = regexp.MustCompile(`Текущая дата UTC: \d{4}-\d{2}-\d{2}`)

func TestFollowUpAnchors(t *testing.T) {
	for _, tc := range []struct {
		text string
		want bool
	}{{"а что дальше", true}, {"а?", false}, {"!да", false}, {"да!", true}, {"что это?", true}, {"iOS", true}, {"не только", false}, {"моя подписка", false}, {"123", true}} {
		if got := IsFollowUp(tc.text); got != tc.want {
			t.Errorf("%q got %v want %v", tc.text, got, tc.want)
		}
	}
}
