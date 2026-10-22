// Package release selects a new image version without overwriting published tags.
package release

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
)

var versionTag = regexp.MustCompile(`^(\d+)\.(\d+)\.(\d+)$`)
var repositoryName = regexp.MustCompile(`^[a-z0-9_-]+/[a-z0-9_.-]+$`)

func Major(path string) (int, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	match := versionTag.FindStringSubmatch(strings.TrimSpace(string(raw)))
	if match == nil {
		return 0, errors.New("VERSION must contain a numeric major.minor.patch version")
	}
	return strconv.Atoi(match[1])
}

func Next(major int, tags []string) (string, error) {
	if major < 0 {
		return "", errors.New("major version must be nonnegative")
	}
	latest := -1
	for _, tag := range tags {
		parts := versionTag.FindStringSubmatch(strings.TrimSpace(tag))
		if parts == nil {
			continue
		}
		m, err := strconv.Atoi(parts[1])
		if err != nil {
			return "", errors.New("published major version overflows")
		}
		if m != major {
			continue
		}
		minor, err := strconv.Atoi(parts[2])
		if err != nil || minor == int(^uint(0)>>1) {
			return "", errors.New("published minor version overflows")
		}
		latest = max(latest, minor)
	}
	return fmt.Sprintf("%d.%d.0", major, latest+1), nil
}

// Tags fails closed on failed, truncated, malformed, looping or off-origin listings.
// Incomplete registry data must never be interpreted as an unused release version.
func Tags(ctx context.Context, client *http.Client, baseURL, repository string) ([]string, error) {
	if !repositoryName.MatchString(repository) {
		return nil, errors.New("invalid image repository")
	}
	base, err := url.Parse(baseURL)
	if err != nil || base.Host == "" {
		return nil, errors.New("invalid registry URL")
	}
	next := strings.TrimRight(baseURL, "/") + "/v2/repositories/" + repository + "/tags?page_size=100"
	tags := []string{}
	seen := map[string]bool{}
	for range 50 {
		if next == "" {
			return tags, nil
		}
		u, err := url.Parse(next)
		if err != nil || u.Host != base.Host || u.Scheme != base.Scheme || u.User != nil {
			return nil, errors.New("registry pagination left its origin")
		}
		if seen[next] {
			return nil, errors.New("registry pagination loop")
		}
		seen[next] = true
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, next, nil)
		if err != nil {
			return nil, errors.New("invalid registry request")
		}
		resp, err := client.Do(req)
		if err != nil {
			return nil, errors.New("could not list registry tags")
		}
		raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
		resp.Body.Close()
		if readErr != nil || resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("registry tag listing failed (status %d)", resp.StatusCode)
		}
		var page struct {
			Results *[]struct {
				Name *string `json:"name"`
			} `json:"results"`
			Next *string `json:"next"`
		}
		if json.Unmarshal(raw, &page) != nil || page.Results == nil {
			return nil, errors.New("invalid registry tag listing")
		}
		for _, item := range *page.Results {
			if item.Name == nil {
				return nil, errors.New("registry tag has no name")
			}
			tags = append(tags, *item.Name)
		}
		next = ""
		if page.Next != nil && *page.Next != "" {
			ref, e := url.Parse(*page.Next)
			if e != nil {
				return nil, errors.New("invalid registry next page")
			}
			next = u.ResolveReference(ref).String()
		}
	}
	if next == "" {
		return tags, nil
	}
	return nil, errors.New("registry tag listing exceeds 50 pages")
}
