package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

func TestCLIFailsBeforeExternalCallsWithoutConsent(t *testing.T) {
	for _, mode := range []string{"behavior", "quality", "retrieval", "compare"} {
		if err := run(context.Background(), []string{mode}); err == nil || !strings.Contains(err.Error(), "confirm-external-api") {
			t.Fatal(mode, err)
		}
	}
	for _, args := range [][]string{{"behavior", "--confirm-external-api", "--runs=0"}, {"quality", "--confirm-external-api", "--concurrency=5"}, {"behavior", "--confirm-external-api", "--threshold=NaN"}, {"compare", "--confirm-external-api", "--database-url=postgres://production.test/prompt_opt"}} {
		if err := run(context.Background(), args); err == nil {
			t.Fatal("invalid CLI accepted", args)
		}
	}
}
func TestQualityCLIReportsRealMockedProviderCalls(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"choices":[{"message":{"content":"Здравствуйте."}}],"usage":{"prompt_tokens":50,"completion_tokens":5}}`)
	}))
	defer server.Close()
	dir := t.TempDir()
	env := filepath.Join(dir, "fixture.env")
	os.WriteFile(env, []byte("ZAI_API_KEY=synthetic\nZAI_BASE_URL="+server.URL+"\n"), 0600)
	cases := filepath.Join(dir, "cases.json")
	os.WriteFile(cases, []byte(`{"cases":[{"name":"greeting","user_message":"hello","must_contain_any":["Здравствуйте"]}]}`), 0600)
	path := filepath.Join(dir, "report.json")
	args := []string{"quality", "--env-file", env, "--cases-file", cases, "--faq", "../../faq/faq.json", "--runs=2", "--concurrency=2", "--report", path, "--confirm-external-api"}
	if e := run(context.Background(), args); e != nil {
		t.Fatal(e)
	}
	if calls.Load() != 2 {
		t.Fatal("missing provider requests", calls.Load())
	}
	raw, e := os.ReadFile(path)
	if e != nil {
		t.Fatal(e)
	}
	var report map[string]any
	if json.Unmarshal(raw, &report) != nil || len(report["results"].([]any)) != 2 || report["provider"] != "zai" {
		t.Fatal(string(raw))
	}
	if e = run(context.Background(), args); e == nil || calls.Load() != 2 {
		t.Fatal("existing report did not prevent provider calls", e, calls.Load())
	}
}
