package llm

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"supportbot/internal/domain"
	"supportbot/internal/mcp"
	"sync"
	"testing"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func httpJSON(status int, body string) *http.Response {
	return &http.Response{StatusCode: status, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(body))}
}

type fakeHistory struct {
	domain.History
	users, assistants, records int
	lastUser, lastAssistant    string
}

func (h *fakeHistory) GetHistory(context.Context, int64) ([]domain.Message, error) {
	return []domain.Message{{Role: "user", Content: "Все серверы n/a"}}, nil
}
func (h *fakeHistory) AddUserMessage(_ context.Context, _ int64, s string) error {
	h.users++
	h.lastUser = s
	return nil
}
func (h *fakeHistory) AddAssistantMessage(_ context.Context, _ int64, s string) error {
	h.assistants++
	h.lastAssistant = s
	return nil
}
func (h *fakeHistory) GetRejectedFAQQuestions(int64) map[string]bool       { return nil }
func (h *fakeHistory) RecordFAQContext(int64, domain.FAQContext, []string) { h.records++ }
func (h *fakeHistory) RejectLastFAQ(int64)                                 {}
func (h *fakeHistory) ClearRejectedFAQsIfNewTopic(int64, string)           {}
func (h *fakeHistory) GetLastUserMessage(int64) string                     { return "Все серверы n/a" }

type fakeFAQ struct {
	calls int
	query string
}

func (f *fakeFAQ) BuildFAQContext(_ context.Context, s string, _ map[string]bool) (domain.FAQContext, error) {
	f.calls++
	f.query = s
	return domain.FAQContext{Text: "FAQ evidence", Results: []domain.FAQResult{{Question: "n/a", Answer: "Обновить подписку"}}}, nil
}

type fakeRouter struct {
	calls int
	err   error
	mu    sync.Mutex
}

func (r *fakeRouter) ListTools() []mcp.Tool {
	return []mcp.Tool{{Name: "example", InputSchema: Object{"type": "object", "properties": Object{}}}}
}
func (r *fakeRouter) CallTool(context.Context, string, map[string]any, int64) (string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls++
	return `{"fact":"active"}`, r.err
}
func TestFallbackCompletedToolCacheAndExactlyOnceHistory(t *testing.T) {
	history := &fakeHistory{}
	faq := &fakeFAQ{}
	router := &fakeRouter{}
	primaryCalls, fallbackCalls := 0, 0
	httpClient := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		raw, _ := io.ReadAll(req.Body)
		if req.URL.Host == "primary" {
			primaryCalls++
			if primaryCalls == 1 {
				return httpJSON(200, `{"output":[{"type":"function_call","name":"example","call_id":"a","arguments":"{\"arg\":1}"}]}`), nil
			}
			return httpJSON(413, `{"error":{"message":"too big"}}`), nil
		}
		fallbackCalls++
		if !strings.Contains(string(raw), "результаты MCP") {
			t.Error("fallback lacks completed tool facts")
		}
		if fallbackCalls == 1 {
			return httpJSON(200, `{"choices":[{"message":{"content":"","tool_calls":[{"id":"b","function":{"name":"example","arguments":"{\"arg\":1}"}}]}}]}`), nil
		}
		if !strings.Contains(string(raw), "active") {
			t.Error("terminal answer lacks tool evidence")
		}
		return httpJSON(200, `{"choices":[{"message":{"content":"Подписка активна."}}]}`), nil
	})}
	p, _ := NewProvider(ProviderConfig{Name: "openai", Model: "gpt-6-luna", BaseURL: "https://primary", APIKey: "test"}, router.ListTools, httpClient)
	q, _ := NewProvider(ProviderConfig{Name: "deepseek", Model: "deepseek-flash", BaseURL: "https://fallback", APIKey: "test"}, router.ListTools, httpClient)
	engine := &Engine{Providers: []*Provider{p, q}, History: history, FAQ: faq, Router: router}
	reply, err := engine.Chat(context.Background(), "iOS", 42)
	if err != nil {
		t.Fatal(err)
	}
	if reply.Text != "Подписка активна." || len(reply.FAQContext.Results) != 1 {
		t.Fatal(reply)
	}
	if router.calls != 1 || faq.calls != 1 || history.users != 1 || history.assistants != 1 || history.records != 1 {
		t.Fatalf("duplicate work: tool=%d FAQ=%d history=%+v", router.calls, faq.calls, history)
	}
	if faq.query != "Все серверы n/a iOS" {
		t.Fatal(faq.query)
	}
	if primaryCalls != 2 || fallbackCalls != 2 {
		t.Fatalf("unexpected provider retry count %d/%d", primaryCalls, fallbackCalls)
	}
}
func TestUnknownToolOutcomeCannotFallbackOrPersist(t *testing.T) {
	router := &fakeRouter{err: errors.New("write response lost")}
	history := &fakeHistory{}
	count := 0
	client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		count++
		return httpJSON(200, `{"output":[{"type":"function_call","name":"example","call_id":"a","arguments":"{}"}]}`), nil
	})}
	p, _ := NewProvider(ProviderConfig{Name: "openai", Model: "gpt-6-luna", BaseURL: "https://primary", APIKey: "test"}, router.ListTools, client)
	engine := &Engine{Providers: []*Provider{p, p}, Router: router, History: history}
	_, err := engine.Chat(context.Background(), "delete", 42)
	if err == nil || FallbackEligible(err) {
		t.Fatalf("unknown outcome eligible for fallback: %v", err)
	}
	if count != 1 || router.calls != 1 || history.users != 0 || history.assistants != 0 {
		t.Fatal("unknown action replayed or persisted")
	}
}
func TestBarePromiseNudgeProducesTerminalAnswer(t *testing.T) {
	router := &fakeRouter{}
	count := 0
	client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		count++
		if count == 1 {
			return httpJSON(200, `{"output":[{"type":"message","content":[{"type":"output_text","text":"Проверю подписку."}]}]}`), nil
		}
		raw, _ := io.ReadAll(req.Body)
		if !strings.Contains(string(raw), "не вызвал ни одного инструмента") {
			t.Error("no promise correction")
		}
		return httpJSON(200, `{"output":[{"type":"message","content":[{"type":"output_text","text":"Подписка активна, доступна до завтра."}]}]}`), nil
	})}
	p, _ := NewProvider(ProviderConfig{Name: "openai", Model: "gpt-6-luna", BaseURL: "https://primary", APIKey: "test"}, router.ListTools, client)
	engine := &Engine{Providers: []*Provider{p}, Router: router}
	reply, err := engine.Chat(context.Background(), "status", 42)
	if err != nil || count != 2 || reply.Text == "Проверю подписку." {
		t.Fatalf("%+v %v", reply, err)
	}
}
func TestImageSkipsIncapablePrimaryAndPDFUsesNativeFiles(t *testing.T) {
	for _, pdf := range []bool{false, true} {
		calls := 0
		history := &fakeHistory{}
		client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
			calls++
			if req.URL.Host != "zai" {
				t.Error("incapable primary received attachment")
			}
			raw, _ := io.ReadAll(req.Body)
			if pdf && !strings.Contains(string(raw), "file_data") {
				t.Error("PDF not encoded as file")
			}
			return httpJSON(200, `{"choices":[{"finish_reason":"stop","message":{"content":"Вижу документ."}}]}`), nil
		})}
		p, _ := NewProvider(ProviderConfig{Name: "groq", Model: "llama", BaseURL: "https://groq", APIKey: "test"}, nil, client)
		q, _ := NewProvider(ProviderConfig{Name: "zai", Model: "glm-5.3-flash", BaseURL: "https://zai", APIKey: "test"}, nil, client)
		engine := &Engine{Providers: []*Provider{p, q}, History: history}
		var err error
		if pdf {
			_, err = engine.ChatWithPDF(context.Background(), "", 42, "YWJj")
		} else {
			_, err = engine.ChatWithImage(context.Background(), "", 42, "YWJj", "image/png")
		}
		if err != nil || calls != 1 {
			t.Fatalf("%v calls=%d", err, calls)
		}
		expected := "[Скриншот]"
		if pdf {
			expected = "[PDF]"
		}
		if history.lastUser != expected {
			t.Fatal(history.lastUser)
		}
	}
}
