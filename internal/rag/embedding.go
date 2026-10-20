package rag

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/rand/v2"
	"net/http"
	"strconv"
	"strings"
	"supportbot/internal/config"
	"supportbot/internal/diagnostics"
	"time"
)

type Embedding interface {
	Dimension() int
	Embed(context.Context, string) ([]float64, error)
	EmbedBatch(context.Context, []string) ([][]float64, error)
	Fingerprint() map[string]any
}
type HTTPEmbedding struct {
	Provider, Key, BaseURL, Model string
	Client                        *http.Client
}

func NewEmbedding(s config.Settings, client *http.Client) Embedding {
	if client == nil {
		client = &http.Client{Timeout: 60 * time.Second}
	}
	p := &HTTPEmbedding{Provider: s.EmbeddingProvider, Client: client}
	if p.Provider == "openai" {
		p.Key = s.OpenAIAPIKey
		p.BaseURL = strings.TrimRight(s.OpenAIBaseURL, "/")
		p.Model = s.OpenAIEmbeddingModel
	} else {
		p.Key = s.GeminiAPIKey
		p.BaseURL = strings.TrimRight(s.GeminiBaseURL, "/")
		p.Model = "gemini-embedding-001"
	}
	diagnostics.Register(p.Key)
	return p
}
func (p *HTTPEmbedding) Dimension() int {
	if p.Provider == "openai" {
		return 1536
	}
	return 2000
}
func (p *HTTPEmbedding) Fingerprint() map[string]any {
	name := "OpenAiEmbeddingProvider"
	if p.Provider == "gemini" {
		name = "GeminiEmbeddingProvider"
	}
	return map[string]any{"provider": "app.rag.embedding." + name, "model": p.Model, "base_url": p.BaseURL, "dimension": p.Dimension()}
}
func (p *HTTPEmbedding) request(ctx context.Context, endpoint string, body any) (map[string]json.RawMessage, error) {
	raw, e := json.Marshal(body)
	if e != nil {
		return nil, e
	}
	var last error
	for attempt := 0; attempt < 3; attempt++ {
		attemptCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
		req, e := http.NewRequestWithContext(attemptCtx, http.MethodPost, p.BaseURL+endpoint, bytes.NewReader(raw))
		if e != nil {
			cancel()
			return nil, e
		}
		req.Header.Set("Content-Type", "application/json")
		if p.Provider == "openai" {
			req.Header.Set("Authorization", "Bearer "+p.Key)
		} else {
			req.Header.Set("x-goog-api-key", p.Key)
		}
		delay := time.Duration(float64(time.Second) * 0.5 * float64(uint64(1)<<attempt) * (0.5 + rand.Float64()/2))
		diagnostics.Payload(ctx, "Embedding HTTP request", map[string]any{"url": req.URL.String(), "headers": req.Header, "body": body}, "provider", p.Provider, "model", p.Model, "attempt", attempt+1)
		started := time.Now()
		resp, e := p.Client.Do(req)
		if e != nil {
			diagnostics.Error(ctx, "Embedding HTTP transport failed", e, "provider", p.Provider, "attempt", attempt+1)
			last = fmt.Errorf("embedding transport failed")
		} else {
			data, readErr := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
			resp.Body.Close()
			cancel()
			diagnostics.Info(ctx, "Embedding HTTP attempt", "provider", p.Provider, "model", p.Model, "attempt", attempt+1, "status_code", resp.StatusCode, "duration_seconds", time.Since(started).Seconds())
			diagnostics.Payload(ctx, "Embedding HTTP response", data, "provider", p.Provider, "model", p.Model, "status_code", resp.StatusCode, "attempt", attempt+1)
			if readErr == nil && resp.StatusCode == 200 {
				var out map[string]json.RawMessage
				if e = json.Unmarshal(data, &out); e != nil {
					return nil, fmt.Errorf("invalid embedding JSON")
				}
				return out, nil
			}
			last = fmt.Errorf("embedding HTTP status %d", resp.StatusCode)
			if raw := resp.Header.Get("Retry-After"); raw != "" {
				if seconds, e := strconv.ParseFloat(strings.TrimSpace(raw), 64); e == nil && seconds >= 0 {
					delay = time.Duration(min(seconds, 8) * float64(time.Second))
				}
			}
			if readErr == nil && !retryableEmbeddingStatus(resp.StatusCode) {
				return nil, last
			}
		}
		cancel()
		if attempt < 2 {
			diagnostics.Info(ctx, "Embedding HTTP retry", "provider", p.Provider, "model", p.Model, "attempt", attempt+2, "delay_seconds", delay.Seconds())
			timer := time.NewTimer(delay)
			select {
			case <-ctx.Done():
				timer.Stop()
				return nil, ctx.Err()
			case <-timer.C:
			}
		}
	}
	return nil, last
}
func (p *HTTPEmbedding) Embed(ctx context.Context, text string) ([]float64, error) {
	if strings.TrimSpace(text) == "" {
		return nil, nil
	}
	if p.Provider == "gemini" {
		out, e := p.request(ctx, "/models/"+p.Model+":embedContent", map[string]any{"content": map[string]any{"parts": []any{map[string]any{"text": text}}}, "outputDimensionality": p.Dimension()})
		if e != nil {
			return nil, e
		}
		var v struct {
			Values []float64 `json:"values"`
		}
		if e = json.Unmarshal(out["embedding"], &v); e != nil {
			return nil, e
		}
		return validVector(v.Values), nil
	}
	out, e := p.request(ctx, "/embeddings", map[string]any{"model": p.Model, "input": text, "dimensions": p.Dimension()})
	if e != nil {
		return nil, e
	}
	var rows []struct {
		Embedding []float64 `json:"embedding"`
	}
	if e = json.Unmarshal(out["data"], &rows); e != nil {
		return nil, e
	}
	if len(rows) == 0 {
		return nil, nil
	}
	return validVector(rows[0].Embedding), nil
}
func (p *HTTPEmbedding) EmbedBatch(ctx context.Context, texts []string) ([][]float64, error) {
	result := make([][]float64, len(texts))
	if len(texts) == 0 {
		return result, nil
	}
	if p.Provider == "gemini" {
		requests := []any{}
		for _, t := range texts {
			requests = append(requests, map[string]any{"model": "models/" + p.Model, "content": map[string]any{"parts": []any{map[string]any{"text": t}}}, "outputDimensionality": p.Dimension()})
		}
		out, e := p.request(ctx, "/models/"+p.Model+":batchEmbedContents", map[string]any{"requests": requests})
		if e != nil {
			return result, e
		}
		var rows []struct {
			Values []float64 `json:"values"`
		}
		if e = json.Unmarshal(out["embeddings"], &rows); e != nil {
			return result, e
		}
		for i, row := range rows {
			if i < len(result) {
				result[i] = validVector(row.Values)
			}
		}
		return result, nil
	}
	out, e := p.request(ctx, "/embeddings", map[string]any{"model": p.Model, "input": texts, "dimensions": p.Dimension()})
	if e != nil {
		return result, e
	}
	var rows []struct {
		Index     *int      `json:"index"`
		Embedding []float64 `json:"embedding"`
	}
	if e = json.Unmarshal(out["data"], &rows); e != nil {
		return result, e
	}
	for _, row := range rows {
		if row.Index != nil && *row.Index >= 0 && *row.Index < len(result) {
			result[*row.Index] = validVector(row.Embedding)
		}
	}
	return result, nil
}
func validVector(v []float64) []float64 {
	for _, n := range v {
		if math.IsNaN(n) || math.IsInf(n, 0) {
			return nil
		}
	}
	return v
}
func VectorString(v []float64) string { b, _ := json.Marshal(v); return string(b) }

func retryableEmbeddingStatus(code int) bool {
	switch code {
	case 408, 429, 500, 502, 503, 504:
		return true
	}
	return false
}
