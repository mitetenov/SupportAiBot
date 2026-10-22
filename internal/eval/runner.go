package eval

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"supportbot/internal/config"
	"supportbot/internal/domain"
	"supportbot/internal/llm"
	"supportbot/internal/mcp"
	"supportbot/internal/rag"
	"time"

	"github.com/joho/godotenv"
)

//go:embed synthetic.json
var syntheticJSON []byte

const SyntheticID int64 = 424242

func (c *BehaviorCase) UnmarshalJSON(raw []byte) error {
	type plain BehaviorCase
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return err
	}
	if fields["history"] == nil && fields["scenario_history"] != nil {
		fields["history"] = fields["scenario_history"]
	}
	if fields["tool_results"] == nil && fields["custom_tool_results"] != nil {
		fields["tool_results"] = fields["custom_tool_results"]
	}
	if fields["expect_escalation"] == nil {
		fields["expect_escalation"] = json.RawMessage(`false`)
	}
	normalized, _ := json.Marshal(fields)
	if err := json.Unmarshal(normalized, (*plain)(c)); err != nil {
		return err
	}
	c.SkipEscalation = string(fields["expect_escalation"]) == "null"
	return nil
}

var relativeDate = regexp.MustCompile(`^\{\{today([+-]\d+)\}\}(T.*)$`)

func ResolveDates(value any, now time.Time) any {
	switch v := value.(type) {
	case map[string]any:
		for key, item := range v {
			v[key] = ResolveDates(item, now)
		}
	case []any:
		for i, item := range v {
			v[i] = ResolveDates(item, now)
		}
	case string:
		if m := relativeDate.FindStringSubmatch(v); m != nil {
			days, _ := strconv.Atoi(m[1])
			return now.UTC().AddDate(0, 0, days).Format("2006-01-02") + m[2]
		}
	}
	return value
}
func LoadCases(path string, now time.Time) ([]BehaviorCase, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var payload any
	if err = json.Unmarshal(raw, &payload); err != nil {
		return nil, err
	}
	raw, err = json.Marshal(ResolveDates(payload, now))
	if err != nil {
		return nil, err
	}
	var result struct {
		Cases []BehaviorCase `json:"cases"`
	}
	if err = json.Unmarshal(raw, &result); err != nil {
		return nil, err
	}
	if len(result.Cases) == 0 {
		return nil, fmt.Errorf("no evaluation cases")
	}
	seen := map[string]bool{}
	for _, c := range result.Cases {
		if c.Name == "" || c.UserMessage == "" || seen[c.Name] {
			return nil, fmt.Errorf("invalid or duplicate evaluation case")
		}
		seen[c.Name] = true
	}
	return result.Cases, nil
}

// Settings does not validate or use bot/MCP/database credentials. Only the selected
// provider is called; synthetic MCP owners below never open network connections.
func Settings(path string) (config.Settings, error) {
	values := map[string]string{}
	if path != "" {
		loaded, e := godotenv.Read(path)
		if e != nil && !os.IsNotExist(e) {
			return config.Settings{}, fmt.Errorf("cannot read evaluation env file")
		}
		if e == nil {
			values = loaded
		}
	}
	for _, item := range os.Environ() {
		key, value, ok := strings.Cut(item, "=")
		if ok {
			values[key] = value
		}
	}
	return config.FromEnv(func(k string) (string, bool) { v, ok := values[k]; return v, ok })
}

type syntheticOwner struct {
	Owner   string         `json:"owner"`
	Tools   []mcp.Tool     `json:"tools"`
	Results map[string]any `json:"results"`
	Trace   *[]Call        `json:"-"`
}

func (s *syntheticOwner) ServerName() string    { return s.Owner }
func (s *syntheticOwner) ListTools() []mcp.Tool { return s.Tools }
func (s *syntheticOwner) CallTool(_ context.Context, name string, args map[string]any) (string, error) {
	*s.Trace = append(*s.Trace, Call{name, args})
	result, ok := s.Results[name]
	if !ok {
		result = map[string]any{"ok": true, "source": s.Owner, "tool": name, "data": map[string]any{}}
	}
	raw, e := json.Marshal(result)
	return string(raw), e
}
func SyntheticRouter(custom map[string]any, trace *[]Call) *mcp.Router {
	var owners []*syntheticOwner
	if err := json.Unmarshal(syntheticJSON, &owners); err != nil {
		panic(err)
	}
	for _, owner := range owners {
		owner.Trace = trace
		for name, value := range custom {
			isBed := strings.HasPrefix(name, "bedolaga_")
			if (owner.Owner == "bedolaga") == isBed {
				owner.Results[name] = value
			}
		}
		if owner.Owner == "remnawave" {
			for _, name := range []string{"nodes_list", "nodes_get"} {
				if _, ok := custom[name]; ok {
					schema := map[string]any{"type": "object", "properties": map[string]any{}}
					if name == "nodes_get" {
						schema["properties"] = map[string]any{"uuid": map[string]any{"type": "string"}}
						schema["required"] = []string{"uuid"}
					}
					owner.Tools = append(owner.Tools, mcp.Tool{Name: name, Description: "Synthetic node diagnostic fixture", InputSchema: schema})
				}
			}
		}
	}
	clients := make([]mcp.Client, len(owners))
	for i, owner := range owners {
		clients[i] = owner
	}
	return mcp.NewRouter(clients, true)
}

type scenarioHistory struct{ messages []domain.Message }

func (h *scenarioHistory) GetHistory(context.Context, int64) ([]domain.Message, error) {
	return append([]domain.Message{}, h.messages...), nil
}
func (h *scenarioHistory) AddUserMessage(_ context.Context, _ int64, s string) error {
	h.messages = append(h.messages, domain.Message{Role: "user", Content: s})
	return nil
}
func (h *scenarioHistory) AddAssistantMessage(_ context.Context, _ int64, s string) error {
	h.messages = append(h.messages, domain.Message{Role: "assistant", Content: s})
	return nil
}
func (h *scenarioHistory) GetLastUserMessage(int64) string {
	for i := len(h.messages) - 1; i >= 0; i-- {
		if h.messages[i].Role == "user" {
			return h.messages[i].Content
		}
	}
	return ""
}
func (h *scenarioHistory) GetRejectedFAQQuestions(int64) map[string]bool       { return nil }
func (h *scenarioHistory) RecordFAQContext(int64, domain.FAQContext, []string) {}
func (h *scenarioHistory) RejectLastFAQ(int64)                                 {}
func (h *scenarioHistory) ClearRejectedFAQsIfNewTopic(int64, string)           {}
func (h *scenarioHistory) Clear(context.Context, int64) error                  { h.messages = nil; return nil }
func (h *scenarioHistory) EvictStaleEntries(context.Context) error             { return nil }
func newHistory(items []map[string]any) *scenarioHistory {
	h := &scenarioHistory{}
	for _, item := range items {
		role := "user"
		if item["role"] == "model" || item["role"] == "assistant" {
			role = "assistant"
		}
		text, _ := item["content"].(string)
		if text == "" {
			if parts, ok := item["parts"].([]any); ok {
				all := []string{}
				for _, part := range parts {
					if s, ok := part.(string); ok {
						all = append(all, s)
					} else if p, ok := part.(map[string]any); ok {
						if s, ok := p["text"].(string); ok {
							all = append(all, s)
						}
					}
				}
				text = strings.Join(all, " ")
			}
		}
		if strings.TrimSpace(text) != "" {
			h.messages = append(h.messages, domain.Message{Role: role, Content: strings.TrimSpace(text)})
		}
	}
	return h
}

type fixtureArticle struct {
	ID       string `json:"id"`
	Question string `json:"question"`
	Answer   string `json:"answer"`
	Image    string `json:"image"`
}
type scenarioFAQ struct {
	entries  []fixtureArticle
	patterns []string
}

func (f *scenarioFAQ) BuildFAQContext(_ context.Context, q string, exclude map[string]bool) (domain.FAQContext, error) {
	results := []domain.FAQResult{}
	patterns := append([]string{}, f.patterns...)
	if len(patterns) == 0 {
		patterns = []string{q}
	}
	for _, pattern := range patterns {
		for mode := 0; mode < 3; mode++ {
			found := false
			for _, e := range f.entries {
				match := e.Question == pattern
				if mode == 1 {
					match = strings.HasPrefix(strings.ToLower(e.Question), strings.ToLower(strings.TrimSpace(pattern)))
				}
				if mode == 2 {
					match = strings.Contains(strings.ToLower(e.Question), strings.ToLower(strings.TrimSpace(pattern)))
				}
				if match {
					if !exclude[e.Question] {
						results = append(results, domain.FAQResult{Question: e.Question, Answer: e.Answer, Image: e.Image, Similarity: 1, RRFScore: 1})
					}
					found = true
					break
				}
			}
			if found {
				break
			}
		}
	}
	// Catalog anchors and context formatting come from the same production helpers.
	for _, id := range rag.RequiredIDs(q) {
		for _, e := range f.entries {
			if e.ID == id && !exclude[e.Question] {
				exists := false
				for _, r := range results {
					exists = exists || r.Question == e.Question
				}
				if !exists {
					results = append(results, domain.FAQResult{Question: e.Question, Answer: e.Answer, Image: e.Image})
				}
			}
		}
	}
	out := domain.FAQContext{Results: results}
	if len(results) > 0 {
		out.Text = rag.FormatFAQContext(results)
		out.BestQuestion = results[0].Question
		for _, r := range results {
			out.MaxSimilarity = max(out.MaxSimilarity, r.Similarity)
		}
	}
	return out, nil
}
func RunCase(ctx context.Context, c BehaviorCase, s config.Settings, client *http.Client, faqPath string) (Result, error) {
	raw, e := os.ReadFile(faqPath)
	if e != nil {
		return Result{}, e
	}
	faq := &scenarioFAQ{patterns: c.FAQCandidates}
	if e = json.Unmarshal(raw, &faq.entries); e != nil {
		return Result{}, e
	}
	for i := range faq.entries {
		if faq.entries[i].ID == "" && faq.entries[i].Question == "Как поделиться подпиской с членом семьи?" {
			faq.entries[i].ID = "subscription_sharing"
		}
	}
	calls := []Call{}
	router := SyntheticRouter(c.ToolResults, &calls)
	engine, e := llm.New(s, router, newHistory(c.History), faq, nil, client)
	if e != nil {
		return Result{}, e
	}
	reply, e := engine.Chat(ctx, c.UserMessage, SyntheticID)
	if e != nil {
		return Result{}, e
	}
	return Score(c, reply.Text, calls), nil
}
