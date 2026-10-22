package eval

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// ReportFile reserves a new path before any API calls and serializes snapshots.
// Existing reports are never truncated, even when two evaluations start together.
type ReportFile struct {
	file *os.File
	mu   sync.Mutex
}

func NewReport(path string) (*ReportFile, error) {
	if path == "" {
		return nil, fmt.Errorf("--report is required")
	}
	if e := os.MkdirAll(filepath.Dir(path), 0755); e != nil {
		return nil, e
	}
	f, e := os.OpenFile(path, os.O_RDWR|os.O_CREATE|os.O_EXCL, 0600)
	if e != nil {
		return nil, fmt.Errorf("report already exists or cannot be created")
	}
	return &ReportFile{file: f}, nil
}
func (r *ReportFile) Save(v any) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	raw, e := json.MarshalIndent(v, "", "  ")
	if e != nil {
		return e
	}
	raw = append(raw, '\n')
	if _, e = r.file.WriteAt(raw, 0); e != nil {
		return e
	}
	if e = r.file.Truncate(int64(len(raw))); e != nil {
		return e
	}
	return r.file.Sync()
}
func (r *ReportFile) Close() error { return r.file.Close() }

type CaseRecord struct {
	Run int `json:"run"`
	Result
	Seconds   float64       `json:"seconds"`
	Requests  []Observation `json:"requests"`
	ErrorType string        `json:"error_type,omitempty"`
}

func Summarize(records []CaseRecord) map[string]any {
	byCase := map[string]map[string]int{}
	requests := []Observation{}
	for _, r := range records {
		if byCase[r.Name] == nil {
			byCase[r.Name] = map[string]int{"passed": 0, "runs": 0}
		}
		byCase[r.Name]["runs"]++
		if r.Passed {
			byCase[r.Name]["passed"]++
		}
		requests = append(requests, r.Requests...)
	}
	sum := func(get func(Observation) *int64) *int64 {
		if len(requests) == 0 {
			return nil
		}
		var total int64
		for _, request := range requests {
			v := get(request)
			if v == nil {
				return nil
			}
			total += *v
		}
		return &total
	}
	return map[string]any{"cases": byCase, "api_requests": len(requests), "usage": Counts{sum(func(r Observation) *int64 { return r.Input }), sum(func(r Observation) *int64 { return r.Output }), sum(func(r Observation) *int64 { return r.Cached })}}
}
