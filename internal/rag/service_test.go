package rag

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"supportbot/internal/config"
	"supportbot/internal/domain"
	"sync"
	"sync/atomic"
	"testing"
)

type fakeEmbedding struct {
	calls atomic.Int64
	fail  bool
	model string
}

func (f *fakeEmbedding) Dimension() int { return 3 }
func (f *fakeEmbedding) Fingerprint() map[string]any {
	return map[string]any{"provider": "fake", "model": f.model, "base_url": "http://fixture", "dimension": 3}
}
func (f *fakeEmbedding) Embed(ctx context.Context, text string) ([]float64, error) {
	f.calls.Add(1)
	if f.fail {
		return nil, fmt.Errorf("offline")
	}
	if strings.Contains(text, "триал") {
		return []float64{0, 1, 0}, nil
	}
	return []float64{1, 0, 0}, nil
}
func (f *fakeEmbedding) EmbedBatch(ctx context.Context, texts []string) ([][]float64, error) {
	out := [][]float64{}
	for _, text := range texts {
		v, e := f.Embed(ctx, text)
		if e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, nil
}

func TestPythonDifferentialFoundation(t *testing.T) {
	raw, e := os.ReadFile("testdata/python_foundation.json")
	if e != nil {
		t.Fatal(e)
	}
	var f struct {
		Queries []struct {
			Input, Clean string
			Required     []string
		}
		Format struct {
			Results []domain.FAQResult
			Text    string
		}
		Fingerprints []struct{ Provider, Fingerprint string }
	}
	if e = json.Unmarshal(raw, &f); e != nil {
		t.Fatal(e)
	}
	for _, row := range f.Queries {
		if got := CleanQuery(row.Input); got != row.Clean {
			t.Errorf("clean %q: %q != %q", row.Input, got, row.Clean)
		}
		if got := RequiredIDs(row.Input); !reflect.DeepEqual(got, row.Required) {
			t.Errorf("catalog %q: %v != %v", row.Input, got, row.Required)
		}
	}
	if got := FormatFAQContext(f.Format.Results); got != f.Format.Text {
		t.Error("FAQ context rendering changed")
	}
	for _, row := range f.Fingerprints {
		s := config.Default()
		if row.Provider == "OpenAiEmbeddingProvider" {
			s.EmbeddingProvider = "openai"
		} else {
			s.EmbeddingProvider = "gemini"
		}
		svc := New(nil, NewEmbedding(s, nil))
		if got := svc.GetIndexFingerprint("source-hash"); got != row.Fingerprint {
			t.Errorf("fingerprint %s: %s != %s", row.Provider, got, row.Fingerprint)
		}
	}
}

func TestEmbeddingBatchReordersOpenAI(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer sentinel" || r.URL.Path != "/embeddings" {
			t.Error("embedding contract headers/path")
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"data":[{"index":1,"embedding":[0,1,0]},{"index":0,"embedding":[1,0,0]}]}`)
	}))
	defer server.Close()
	p := &HTTPEmbedding{Provider: "openai", Key: "sentinel", BaseURL: server.URL, Model: "fixture", Client: server.Client()}
	vectors, e := p.EmbedBatch(context.Background(), []string{"a", "b"})
	if e != nil || !reflect.DeepEqual(vectors, [][]float64{{1, 0, 0}, {0, 1, 0}}) {
		t.Fatal("out-of-order embedding response", vectors, e)
	}
}
func TestLRUHitsAndConcurrentDuplicateInsertion(t *testing.T) {
	f := New(nil, &fakeEmbedding{})
	ctx := context.Background()
	for i := 0; i < 256; i++ {
		if _, e := f.EmbedQuery(ctx, fmt.Sprint(i)); e != nil {
			t.Fatal(e)
		}
	}
	f.EmbedQuery(ctx, "0")
	f.EmbedQuery(ctx, "new")
	if _, ok := f.cache["0"]; !ok {
		t.Fatal("hot query evicted")
	}
	if _, ok := f.cache["1"]; ok {
		t.Fatal("cold query retained")
	}
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); f.EmbedQuery(ctx, "same") }()
	}
	wg.Wait()
	seen := map[string]bool{}
	for _, key := range f.cacheOrder {
		if seen[key] {
			t.Fatal("duplicate LRU entry", key)
		}
		seen[key] = true
	}
	if len(f.cacheOrder) != len(f.cache) {
		t.Fatal("cache/order diverged")
	}
}

type embeddingTransport func(*http.Request) (*http.Response, error)

func (f embeddingTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

type brokenEmbeddingBody struct{}

func (brokenEmbeddingBody) Read([]byte) (int, error) { return 0, io.ErrUnexpectedEOF }
func (brokenEmbeddingBody) Close() error             { return nil }
func TestEmbeddingRetryStatusAndReadFailure(t *testing.T) {
	for _, tc := range []struct {
		code   int
		broken bool
		calls  int
	}{{408, false, 2}, {501, false, 1}, {200, true, 2}} {
		t.Run(fmt.Sprint(tc.code, tc.broken), func(t *testing.T) {
			calls := 0
			p := &HTTPEmbedding{Provider: "openai", Key: "fixture", BaseURL: "https://fixture", Model: "fixture", Client: &http.Client{Transport: embeddingTransport(func(r *http.Request) (*http.Response, error) {
				calls++
				resp := &http.Response{StatusCode: 200, Header: http.Header{"Retry-After": []string{"0.001"}}, Body: io.NopCloser(strings.NewReader(`{"data":[{"embedding":[1,0,0]}]}`))}
				if calls == 1 {
					resp.StatusCode = tc.code
					if tc.broken {
						resp.Body = brokenEmbeddingBody{}
					}
				}
				return resp, nil
			})}}
			_, e := p.Embed(context.Background(), "text")
			if calls != tc.calls {
				t.Fatalf("attempts=%d expected=%d error=%v", calls, tc.calls, e)
			}
		})
	}
}
