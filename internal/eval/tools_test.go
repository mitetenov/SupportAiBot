package eval

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"supportbot/internal/config"
	"supportbot/internal/domain"
	"testing"
	"time"
)

func TestObserverUsageAndPrivacy(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"usage":{"prompt_tokens":50,"completion_tokens":5}}`)
	}))
	defer server.Close()
	observer := &Observer{Base: server.Client().Transport}
	client := &http.Client{Transport: observer}
	for range 2 {
		req, _ := http.NewRequest("POST", server.URL, strings.NewReader(`{"messages":[{"role":"user","content":"synthetic-question"}]}`))
		req.Header.Set("Authorization", "Bearer synthetic-secret")
		response, e := client.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		raw, _ := io.ReadAll(response.Body)
		response.Body.Close()
		if !strings.Contains(string(raw), "usage") {
			t.Fatal("observer consumed response")
		}
	}
	records := observer.Records()
	summary := Summarize([]CaseRecord{{Result: Result{Name: "case", Passed: true}, Requests: records}})
	raw, _ := json.Marshal(records)
	if strings.Contains(string(raw), "synthetic-secret") || strings.Contains(string(raw), "synthetic-question") {
		t.Fatal("observer leaked payload")
	}
	if summary["api_requests"] != 2 {
		t.Fatal(summary)
	}
	usage := summary["usage"].(Counts)
	if usage.Input == nil || *usage.Input != 100 || usage.Output == nil || *usage.Output != 10 || usage.Cached != nil {
		t.Fatal(usage)
	}
	for _, raw := range []string{`{"usage":{"prompt_tokens":true,"completion_tokens":-1}}`, `{"usage":{"prompt_tokens":1.1}}`} {
		var v map[string]any
		json.Unmarshal([]byte(raw), &v)
		got := UsageCounts(v)
		if got.Input != nil || got.Output != nil {
			t.Fatal("invalid counters accepted")
		}
	}
	var v map[string]any
	json.Unmarshal([]byte(`{"usage":{"prompt_tokens_details":{"cached_tokens":0}}}`), &v)
	if c := UsageCounts(v); c.Cached == nil || *c.Cached != 0 {
		t.Fatal("known zero lost")
	}
	base := map[string]any{"messages": []any{map[string]any{"role": "system", "content": "rules"}, map[string]any{"role": "user", "content": "hello"}}}
	before := ContextSizes(base)
	base["messages"] = append(base["messages"].([]any), map[string]any{"role": "tool", "content": "large result"})
	after := ContextSizes(base)
	if before["system"] != after["system"] || before["conversation"] != after["conversation"] || after["tool_results"] == 0 {
		t.Fatal(before, after)
	}
}
func TestFailedAndUnknownUsageRemainInSummary(t *testing.T) {
	n := int64(100)
	s := Summarize([]CaseRecord{{Result: Result{Name: "same", Passed: true}, Requests: []Observation{{Counts: Counts{Input: &n}}}}, {Result: Result{Name: "same", Passed: false}, Requests: []Observation{{Status: 503}}}})
	if s["usage"].(Counts).Input != nil || s["cases"].(map[string]map[string]int)["same"]["runs"] != 2 {
		t.Fatal(s)
	}
}
func TestReportRefusesOverwriteAndKeepsSnapshot(t *testing.T) {
	p := filepath.Join(t.TempDir(), "report.json")
	r, e := NewReport(p)
	if e != nil {
		t.Fatal(e)
	}
	defer r.Close()
	if e = r.Save(map[string]int{"partial": 1}); e != nil {
		t.Fatal(e)
	}
	if _, e = NewReport(p); e == nil {
		t.Fatal("report overwritten")
	}
	raw, _ := os.ReadFile(p)
	if !json.Valid(raw) || !strings.Contains(string(raw), "partial") {
		t.Fatal(string(raw))
	}
}
func TestDisposableDatabaseGuard(t *testing.T) {
	for _, dsn := range []string{"postgres://user@production.test/prompt_opt", "postgres://user@127.0.0.1/vpnsupport", "postgres://user@127.0.0.1/postgres", "postgres://user@127.0.0.1/prompt_opt?host=production.test"} {
		if ValidateDatabaseURL(dsn) == nil {
			t.Fatal("unsafe database accepted", dsn)
		}
	}
	for _, dsn := range []string{"postgresql://local:synthetic@127.0.0.1:55493/prompt_opt", "postgres://user@[::1]/prompt_opt_2", "postgresql+asyncpg://local:synthetic@localhost/prompt_opt"} {
		if e := ValidateDatabaseURL(dsn); e != nil {
			t.Fatal(e)
		}
	}
}
func TestMergeModesAreIndependent(t *testing.T) {
	groups := [][]domain.FAQResult{{{Question: "primary", RRFScore: .02}}, {{Question: "fallback", RRFScore: .03}, {Question: "primary", RRFScore: .04}}}
	for mode, want := range map[string][]string{"append": {"primary", "fallback"}, "compete": {"primary", "fallback"}, "off": {"primary"}} {
		if got := Titles(MergeResults(groups, mode)); !reflect.DeepEqual(got, want) {
			t.Fatal(mode, got, want)
		}
	}
	groups[1] = groups[1][:1]
	if got := Titles(MergeResults(groups, "compete")); got[0] != "fallback" {
		t.Fatal(got)
	}
	if got := Titles(MergeResults(groups, "append")); got[0] != "primary" {
		t.Fatal(got)
	}
}
func TestFixtureDatesAndAliases(t *testing.T) {
	p := filepath.Join(t.TempDir(), "cases.json")
	os.WriteFile(p, []byte(`{"cases":[{"name":"x","user_message":"hello","scenario_history":[{"role":"model","parts":[{"text":"prior"}]}],"custom_tool_results":{"x":{"date":"{{today+30}}T12:00:00Z"}}}]}`), 0600)
	cases, e := LoadCases(p, time.Date(2026, 10, 23, 0, 0, 0, 0, time.UTC))
	if e != nil {
		t.Fatal(e)
	}
	raw, _ := json.Marshal(cases)
	if !strings.Contains(string(raw), "2026-11-22") || len(cases[0].History) != 1 || cases[0].ExpectEscalation == nil || *cases[0].ExpectEscalation {
		t.Fatal(string(raw))
	}
	history := newHistory(cases[0].History)
	if history.messages[0].Content != "prior" || history.messages[0].Role != "assistant" {
		t.Fatal(history)
	}
}
func TestEvaluationUsesProductionRouterAndPinnedIdentity(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		var payload map[string]any
		json.NewDecoder(r.Body).Decode(&payload)
		if requests == 1 {
			if len(payload["tools"].([]any)) != 6 {
				t.Error("tool schemas missing")
			}
			fmt.Fprint(w, `{"choices":[{"message":{"tool_calls":[{"id":"x","function":{"name":"bedolaga_billing_get","arguments":"{\"telegram_id\":999999}"}}]}}]}`)
		} else {
			fmt.Fprint(w, `{"choices":[{"message":{"content":"Здравствуйте."}}]}`)
		}
	}))
	defer server.Close()
	settings := config.Default()
	settings.LLMProvider = "zai"
	settings.ZAIAPIKey = "synthetic"
	settings.ZAIBaseURL = server.URL
	settings.LLMFallbackChain = nil
	no := false
	got, e := RunCase(context.Background(), BehaviorCase{Name: "identity", UserMessage: "hello", ExpectedTools: []string{"bedolaga_billing_get"}, ExpectEscalation: &no}, settings, server.Client(), "../../faq/faq.json")
	if e != nil || !got.Passed || requests != 2 {
		t.Fatal(got, e, requests)
	}
}
