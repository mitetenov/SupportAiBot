package llm

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestHTTPRetryAndBusinessErrorMapping(t *testing.T) {
	for _, tc := range []struct {
		provider, body   string
		status, attempts int
		fallback         bool
	}{{"openai", `{"error":{"message":"too big"}}`, 413, 1, true}, {"deepseek", `{"error":{"message":"temporary"}}`, 429, 3, true}, {"zai", `{"error":{"code":"1113","message":"balance"}}`, 200, 1, true}, {"zai", `{"code":"1210","msg":"invalid params"}`, 200, 1, false}, {"openrouter", `{"error":{"code":429,"message":"busy"}}`, 200, 1, true}, {"openrouter", `{"choices":[{"error":{"code":"503","message":"busy"}}]}`, 200, 1, true}, {"openrouter", `{"error":null}`, 200, 1, false}} {
		t.Run(tc.provider+tc.body, func(t *testing.T) {
			calls := 0
			p, _ := NewProvider(ProviderConfig{Name: tc.provider, Model: "custom", APIKey: "test", BaseURL: "https://test"}, nil, &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				calls++
				res := httpJSON(tc.status, tc.body)
				res.Header.Set("Retry-After", "0.001")
				return res, nil
			})})
			_, err := p.Call(context.Background(), nil)
			if err == nil {
				t.Fatal("provider error accepted")
			}
			if FallbackEligible(err) != tc.fallback || calls != tc.attempts {
				t.Fatalf("err=%v fallback=%v calls=%d", err, FallbackEligible(err), calls)
			}
		})
	}
}
func TestProviderHTTPAuthAndPaths(t *testing.T) {
	for _, provider := range []string{"openai", "gemini", "deepseek", "groq", "openrouter", "zai"} {
		t.Run(provider, func(t *testing.T) {
			p, _ := NewProvider(ProviderConfig{Name: provider, Model: "custom", APIKey: "test-secret", BaseURL: "https://fixture/v1"}, nil, &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				path := "/v1/chat/completions"
				if provider == "openai" {
					path = "/v1/responses"
				}
				if provider == "gemini" {
					path = "/v1/models/custom:generateContent"
					if req.Header.Get("x-goog-api-key") != "test-secret" {
						t.Error("Gemini auth contract")
					}
				} else if req.Header.Get("Authorization") != "Bearer test-secret" {
					t.Error("bearer auth contract")
				}
				if req.URL.Path != path {
					t.Error(req.URL.Path)
				}
				raw, _ := io.ReadAll(req.Body)
				if strings.Contains(string(raw), "test-secret") {
					t.Error("key embedded in body")
				}
				return httpJSON(200, `{}`), nil
			})})
			if _, err := p.Call(context.Background(), nil); err != nil {
				t.Fatal(err)
			}
		})
	}
}
func TestCancellationDoesNotAttemptFallback(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	count := 0
	client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) { count++; return nil, req.Context().Err() })}
	p, _ := NewProvider(ProviderConfig{Name: "openai", Model: "custom", APIKey: "test", BaseURL: "https://fixture"}, nil, client)
	engine := &Engine{Providers: []*Provider{p, p}}
	if _, err := engine.Chat(ctx, "request", 42); err != context.Canceled {
		t.Fatal(err)
	}
	if count > 1 {
		t.Fatal("cancelled turn retried")
	}
}
