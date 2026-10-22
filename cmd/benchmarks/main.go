// benchmarks evaluates behavior, request cost and retrieval through Go services.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"supportbot/internal/eval"
	"supportbot/internal/llm"
	"supportbot/internal/rag"
	"supportbot/internal/storage"
	"sync"
	"syscall"
	"time"
	"unicode/utf8"
)

type stringsFlag []string

func (s *stringsFlag) String() string     { return strings.Join(*s, ",") }
func (s *stringsFlag) Set(v string) error { *s = append(*s, v); return nil }
func behavior(ctx context.Context, mode string, args []string) error {
	fs := flag.NewFlagSet(mode, flag.ContinueOnError)
	label := fs.String("label", mode, "report label")
	path := fs.String("report", "", "new JSON report path")
	env := fs.String("env-file", ".env", "provider env file; empty uses environment only")
	runs := fs.Int("runs", 3, "runs per scenario")
	concurrency := fs.Int("concurrency", 2, "parallel scenarios (1..4)")
	threshold := fs.Float64("threshold", .8, "minimum pass rate per case")
	casesPath := fs.String("cases-file", "benchmarks/agent_behavior_cases.json", "scenario fixture")
	faq := fs.String("faq", "faq/faq.json", "FAQ fixture")
	confirmed := fs.Bool("confirm-external-api", false, "allow sending synthetic fixtures to selected LLM provider")
	var selected stringsFlag
	fs.Var(&selected, "case", "case name (repeatable)")
	if e := fs.Parse(args); e != nil {
		return e
	}
	if fs.NArg() != 0 {
		return fmt.Errorf("unexpected positional arguments")
	}
	if !*confirmed {
		return fmt.Errorf("live evaluation requires --confirm-external-api")
	}
	if *runs < 1 || *concurrency < 1 || *concurrency > 4 || *threshold <= 0 || *threshold > 1 || math.IsNaN(*threshold) {
		return fmt.Errorf("invalid runs, concurrency or threshold")
	}
	cases, e := eval.LoadCases(*casesPath, time.Now())
	if e != nil {
		return e
	}
	if len(selected) > 0 {
		filtered := []eval.BehaviorCase{}
		for _, name := range selected {
			found := false
			for _, c := range cases {
				if c.Name == name {
					filtered = append(filtered, c)
					found = true
					break
				}
			}
			if !found {
				return fmt.Errorf("unknown case: %s", name)
			}
		}
		cases = filtered
	}
	settings, e := eval.Settings(*env)
	if e != nil {
		return e
	}
	if mode == "quality" {
		settings.LLMProvider = "zai"
		settings.ZAIModel = "glm-5.3-flash"
		settings.LLMFallbackChain = nil
	}
	for _, target := range settings.LLMProviderTargets() {
		if settings.APIKey(target.Provider) == "" {
			return fmt.Errorf("missing %s API key", target.Provider)
		}
	}
	report, e := eval.NewReport(*path)
	if e != nil {
		return e
	}
	defer report.Close()
	promptHash := sha256.Sum256([]byte(llm.SystemPrompt))
	faqHash, e := eval.HashFile(*faq)
	if e != nil {
		return e
	}
	casesHash, e := eval.HashFile(*casesPath)
	if e != nil {
		return e
	}
	records := []eval.CaseRecord{}
	payload := map[string]any{"label": *label, "provider": settings.LLMProvider, "model": settings.Model(settings.LLMProvider), "reasoning_effort": settings.ReasoningEffort, "prompt_chars": utf8.RuneCountInString(llm.SystemPrompt), "prompt_sha256": hex.EncodeToString(promptHash[:]), "faq_sha256": faqHash, "cases_sha256": casesHash, "resolved_cases": cases, "results": records}
	if e = report.Save(payload); e != nil {
		return e
	}
	type job struct {
		run int
		c   eval.BehaviorCase
	}
	jobs := make(chan job)
	var wg sync.WaitGroup
	var mu sync.Mutex
	var saveError error
	for range *concurrency {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for job := range jobs {
				started := time.Now()
				observer := &eval.Observer{}
				client := &http.Client{Transport: observer, Timeout: 180 * time.Second}
				result, err := eval.RunCase(ctx, job.c, settings, client, *faq)
				errorType := ""
				if err != nil {
					result = eval.Result{Name: job.c.Name, Passed: false, Tools: []string{}, Violations: []string{"provider evaluation failed"}}
					errorType = "evaluation_error"
				}
				record := eval.CaseRecord{Run: job.run, Result: result, Seconds: time.Since(started).Seconds(), Requests: observer.Records(), ErrorType: errorType}
				mu.Lock()
				records = append(records, record)
				payload["results"] = records
				payload["summary"] = eval.Summarize(records)
				if e := report.Save(payload); e != nil && saveError == nil {
					saveError = e
				}
				fmt.Printf("%d/%d %t %s: %d API requests\n", job.run, *runs, result.Passed, result.Name, len(record.Requests))
				mu.Unlock()
			}
		}()
	}
send:
	for run := 1; run <= *runs; run++ {
		for _, c := range cases {
			select {
			case jobs <- job{run, c}:
			case <-ctx.Done():
				break send
			}
		}
	}
	close(jobs)
	wg.Wait()
	if saveError != nil {
		return saveError
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	counts := map[string][2]int{}
	for _, record := range records {
		v := counts[record.Name]
		v[1]++
		if record.Passed {
			v[0]++
		}
		counts[record.Name] = v
	}
	for name, v := range counts {
		if float64(v[0])/float64(v[1]) < *threshold || mode == "quality" && v[0] != v[1] {
			return fmt.Errorf("behavioral contract failed: %s (%d/%d)", name, v[0], v[1])
		}
	}
	return nil
}
func retrieval(ctx context.Context, mode string, args []string) error {
	fs := flag.NewFlagSet(mode, flag.ContinueOnError)
	path := fs.String("report", "", "new JSON report path")
	env := fs.String("env-file", ".env", "embedding env file; empty uses environment only")
	faq := fs.String("faq", "faq/faq.json", "candidate FAQ")
	baseline := fs.String("baseline-faq", "", "baseline FAQ for compare")
	defaultCases := "benchmarks/retrieval_queries.json"
	if mode == "compare" {
		defaultCases = "benchmarks/prompt_retrieval_cases.json"
	}
	casesPath := fs.String("cases", defaultCases, "query fixture")
	dsn := fs.String("database-url", "", "disposable loopback prompt_opt* database")
	confirmed := fs.Bool("confirm-external-api", false, "allow sending FAQ/queries to embedding provider")
	reindex := fs.Bool("reindex", false, "rebuild FAQ vectors")
	detailAt := fs.Float64("detail-at", -1, "similarity for detailed output")
	detailMode := fs.String("detail-mode", "append", "fallback mode for detailed output")
	if e := fs.Parse(args); e != nil {
		return e
	}
	if !*confirmed {
		return fmt.Errorf("live retrieval requires --confirm-external-api")
	}
	if e := eval.ValidateDatabaseURL(*dsn); e != nil {
		return e
	}
	if *detailMode != "append" && *detailMode != "off" && *detailMode != "compete" {
		return fmt.Errorf("invalid detail mode")
	}
	if mode == "compare" && *baseline == "" {
		return fmt.Errorf("--baseline-faq is required")
	}
	cases, e := eval.LoadQueries(*casesPath)
	if e != nil {
		return e
	}
	settings, e := eval.Settings(*env)
	if e != nil {
		return e
	}
	if mode == "compare" {
		settings.EmbeddingProvider = "openai"
		settings.OpenAIBaseURL = "https://api.openai.com/v1"
		settings.OpenAIEmbeddingModel = "text-embedding-3-small"
	}
	if settings.APIKey(settings.EmbeddingProvider) == "" {
		return fmt.Errorf("embedding API key required")
	}
	report, e := eval.NewReport(*path)
	if e != nil {
		return e
	}
	defer report.Close()
	store, e := storage.New(ctx, *dsn)
	if e != nil {
		return fmt.Errorf("test database unavailable")
	}
	defer store.Close()
	embedding := eval.CachedEmbedding(rag.NewEmbedding(settings, nil))
	casesHash, e := eval.HashFile(*casesPath)
	if e != nil {
		return e
	}
	runs := map[string]any{}
	payload := map[string]any{"cases_sha256": casesHash, "model": embedding.Fingerprint(), "runs": runs}
	if e = report.Save(payload); e != nil {
		return e
	}
	if mode == "compare" {
		for _, rev := range []struct{ label, path string }{{"baseline", *baseline}, {"candidate", *faq}} {
			retriever, e := eval.NewRetriever(ctx, store, embedding, rev.path, true)
			if e != nil {
				return e
			}
			rows := []eval.RetrievalRow{}
			correct, top1, chars := 0, 0, 0
			for _, c := range cases {
				raw, found, e := retriever.Retrieve(ctx, c.Query, .45, 3, "append", rev.label == "candidate")
				if e != nil {
					return e
				}
				text := eval.BaselineContext(raw)
				if rev.label == "candidate" && len(found) > 0 {
					text = rag.FormatFAQContext(found)
				}
				titles := eval.Titles(found)
				hit := len(c.Expected) == 0 && len(titles) == 0
				first := false
				for _, title := range titles {
					for _, want := range c.Expected {
						hit = hit || title == want
					}
				}
				if len(titles) > 0 {
					for _, want := range c.Expected {
						first = first || titles[0] == want
					}
				}
				if hit {
					correct++
				}
				if first {
					top1++
				}
				n := utf8.RuneCountInString(text)
				chars += n
				rows = append(rows, eval.RetrievalRow{QueryCase: c, Titles: titles, RawTitles: eval.Titles(raw), ContextChars: n, Hit: hit, Top1: first})
			}
			hash, e := eval.HashFile(rev.path)
			if e != nil {
				return e
			}
			runs[rev.label] = map[string]any{"faq_sha256": hash, "entries": len(retriever.Entries), "results": rows, "correct": correct, "top1": top1, "context_chars": chars}
			if e = report.Save(payload); e != nil {
				return e
			}
			fmt.Printf("%s correct=%d top1=%d context_chars=%d\n", rev.label, correct, top1, chars)
		}
		return nil
	}
	retriever, e := eval.NewRetriever(ctx, store, embedding, *faq, *reindex)
	if e != nil {
		return e
	}
	grid := [][2]float64{{.65, 3}, {.60, 3}, {.55, 3}, {.50, 3}, {.45, 3}, {.65, 5}, {.60, 5}, {.55, 5}, {.50, 5}, {.45, 5}, {.40, 5}, {.35, 5}}
	for _, fallback := range []string{"compete", "append", "off"} {
		for _, point := range grid {
			score := eval.RetrievalScore{}
			details := []map[string]any{}
			for _, c := range cases {
				_, found, e := retriever.Retrieve(ctx, c.Query, point[0], int(point[1]), fallback, true)
				if e != nil {
					return e
				}
				titles := eval.Titles(found)
				score.Add(c, titles)
				details = append(details, map[string]any{"case": c, "titles": titles})
			}
			key := fmt.Sprintf("%s/%.2f/%d", fallback, point[0], int(point[1]))
			runs[key] = map[string]any{"score": score, "details": details, "production_defaults": fallback == "append" && point[0] == .45 && point[1] == 3}
			if e = report.Save(payload); e != nil {
				return e
			}
			fmt.Printf("%s hit@1=%d/%d hit@k=%d/%d held@k=%d/%d controls=%d/%d\n", key, score.HitAt1, score.Positives, score.HitAtK, score.Positives, score.HeldHitAtK, score.HeldPositives, score.ControlFalseMatches, score.Controls)
			if *detailAt == point[0] && *detailMode == fallback && point[1] == 5 {
				for _, detail := range details {
					fmt.Println(detail)
				}
			}
		}
	}
	return nil
}
func run(ctx context.Context, args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: benchmarks behavior|quality|retrieval|compare [flags]")
	}
	switch args[0] {
	case "behavior", "quality":
		return behavior(ctx, args[0], args[1:])
	case "retrieval", "compare":
		return retrieval(ctx, args[0], args[1:])
	}
	return fmt.Errorf("unknown benchmark command")
}
func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(io.Discard, nil)))
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if e := run(ctx, os.Args[1:]); e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
}
