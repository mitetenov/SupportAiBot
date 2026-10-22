package eval

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"sync"
	"time"
	"unicode/utf8"
)

type Counts struct {
	Input  *int64 `json:"input_tokens"`
	Output *int64 `json:"output_tokens"`
	Cached *int64 `json:"cached_input_tokens"`
}
type Observation struct {
	ContextChars map[string]int `json:"context_chars"`
	Status       int            `json:"status"`
	Seconds      float64        `json:"seconds"`
	Counts
}
type Observer struct {
	Base    http.RoundTripper
	mu      sync.Mutex
	records []Observation
}

func jsonSize(v any) int {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
	return utf8.RuneCount(bytes.TrimSuffix(b.Bytes(), []byte("\n")))
}
func ContextSizes(payload map[string]any) map[string]int {
	sizes := map[string]int{"system": 0, "dynamic": 0, "conversation": 0, "tool_results": 0}
	messages, _ := payload["messages"].([]any)
	if messages == nil {
		messages, _ = payload["input"].([]any)
	}
	for i, item := range messages {
		m, ok := item.(map[string]any)
		if !ok {
			continue
		}
		group := "conversation"
		if m["role"] == "tool" || m["type"] == "function_call_output" {
			group = "tool_results"
		} else if m["role"] == "system" {
			group = "dynamic"
			if i == 0 {
				group = "system"
			}
		}
		sizes[group] += jsonSize(m)
	}
	if instructions, ok := payload["instructions"].(string); ok {
		sizes["system"] += utf8.RuneCountInString(instructions)
	}
	tools := payload["tools"]
	if tools == nil {
		tools = []any{}
	}
	sizes["tool_definitions"] = jsonSize(tools)
	return sizes
}
func UsageCounts(payload map[string]any) Counts {
	usage, _ := payload["usage"].(map[string]any)
	details, _ := usage["prompt_tokens_details"].(map[string]any)
	number := func(v any) *int64 {
		switch n := v.(type) {
		case float64:
			if n >= 0 && n == float64(int64(n)) {
				v := int64(n)
				return &v
			}
		}
		return nil
	}
	c := Counts{number(usage["prompt_tokens"]), number(usage["completion_tokens"]), number(details["cached_tokens"])}
	if c.Input == nil {
		c.Input = number(usage["input_tokens"])
	}
	if c.Output == nil {
		c.Output = number(usage["output_tokens"])
	}
	return c
}
func (o *Observer) RoundTrip(req *http.Request) (*http.Response, error) {
	var raw []byte
	var err error
	if req.Body != nil {
		raw, err = io.ReadAll(req.Body)
	}
	if err != nil {
		return nil, err
	}
	req.Body = io.NopCloser(bytes.NewReader(raw))
	var payload map[string]any
	_ = json.Unmarshal(raw, &payload)
	started := time.Now()
	base := o.Base
	if base == nil {
		base = http.DefaultTransport
	}
	resp, err := base.RoundTrip(req)
	record := Observation{ContextChars: ContextSizes(payload), Seconds: time.Since(started).Seconds()}
	if resp != nil {
		record.Status = resp.StatusCode
		body, readErr := io.ReadAll(resp.Body)
		resp.Body.Close()
		resp.Body = io.NopCloser(bytes.NewReader(body))
		if readErr != nil && err == nil {
			err = readErr
		}
		var result map[string]any
		if json.Unmarshal(body, &result) == nil {
			record.Counts = UsageCounts(result)
		}
	}
	record.Seconds = time.Since(started).Seconds()
	o.mu.Lock()
	o.records = append(o.records, record)
	o.mu.Unlock()
	return resp, err
}
func (o *Observer) Records() []Observation {
	o.mu.Lock()
	defer o.mu.Unlock()
	return append([]Observation{}, o.records...)
}
