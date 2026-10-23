package eval

import (
	"fmt"
	"net/url"
	"sort"
	"strings"
	"supportbot/internal/domain"

	"github.com/jackc/pgx/v5"
)

func ValidateDatabaseURL(dsn string) error {
	u, e := url.Parse(strings.Replace(dsn, "postgresql+asyncpg:", "postgresql:", 1))
	if e != nil {
		return fmt.Errorf("use a disposable loopback PostgreSQL database named prompt_opt*")
	}
	if (u.Scheme != "postgres" && u.Scheme != "postgresql") || (u.Hostname() != "127.0.0.1" && u.Hostname() != "localhost" && u.Hostname() != "::1") || !strings.HasPrefix(strings.TrimPrefix(u.Path, "/"), "prompt_opt") {
		return fmt.Errorf("use a disposable loopback PostgreSQL database named prompt_opt*")
	}
	cfg, err := pgx.ParseConfig(strings.Replace(dsn, "postgresql+asyncpg:", "postgresql:", 1))
	if err != nil || (cfg.Host != "127.0.0.1" && cfg.Host != "localhost" && cfg.Host != "::1") || !strings.HasPrefix(cfg.Database, "prompt_opt") {
		return fmt.Errorf("use a disposable loopback PostgreSQL database named prompt_opt*")
	}
	for _, fallback := range cfg.Fallbacks {
		if fallback.Host != "127.0.0.1" && fallback.Host != "localhost" && fallback.Host != "::1" {
			return fmt.Errorf("database fallback must be loopback")
		}
	}
	return nil
}

func MergeResults(groups [][]domain.FAQResult, mode string) []domain.FAQResult {
	out := []domain.FAQResult{}
	indices := map[string]int{}
	if mode == "off" && len(groups) > 1 {
		groups = groups[:1]
	}
	for _, group := range groups {
		group = append([]domain.FAQResult{}, group...)
		sort.SliceStable(group, func(i, j int) bool { return group[i].RRFScore > group[j].RRFScore })
		for _, hit := range group {
			if i, ok := indices[hit.Question]; ok {
				if mode == "compete" && hit.RRFScore > out[i].RRFScore {
					out[i] = hit
				}
				continue
			}
			indices[hit.Question] = len(out)
			out = append(out, hit)
		}
	}
	if mode == "compete" {
		sort.SliceStable(out, func(i, j int) bool { return out[i].RRFScore > out[j].RRFScore })
	}
	return out[:min(5, len(out))]
}

func Titles(results []domain.FAQResult) []string {
	out := []string{}
	for _, r := range results {
		out = append(out, r.Question)
	}
	return out
}
