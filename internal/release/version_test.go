package release

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNextVersion(t *testing.T) {
	for _, tc := range []struct {
		tags []string
		want string
	}{
		{nil, "2.0.0"}, {[]string{"latest", "2.9.0", "2.10.0", "2.11.0-rc1", "3.99.0", " 2.1.7 "}, "2.11.0"},
		{[]string{"2.0.1", "2.0.0", "2.4.99"}, "2.5.0"},
	} {
		got, e := Next(2, tc.tags)
		if e != nil || got != tc.want {
			t.Fatalf("%v -> %q %v; want %s", tc.tags, got, e, tc.want)
		}
	}
	if _, err := Next(2, []string{"2.999999999999999999999.0"}); err == nil {
		t.Fatal("overflow accepted")
	}
}
func TestMajor(t *testing.T) {
	p := filepath.Join(t.TempDir(), "VERSION")
	for _, v := range []string{"2.0.1\n", "x.0.0", "2.bad.0", "2.0", "-1.0.0"} {
		os.WriteFile(p, []byte(v), 0600)
		n, e := Major(p)
		if strings.HasPrefix(v, "2.0.1") {
			if n != 2 || e != nil {
				t.Fatal(n, e)
			}
		} else if e == nil {
			t.Fatal("invalid version accepted", v)
		}
	}
}
func TestRegistryPagination(t *testing.T) {
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Query().Get("page") == "2" {
			fmt.Fprint(w, `{"results":[{"name":"2.10.0"}],"next":null}`)
		} else {
			fmt.Fprint(w, `{"results":[{"name":"2.9.0"}],"next":"?page=2"}`)
		}
	}))
	defer server.Close()
	tags, err := Tags(context.Background(), server.Client(), server.URL, "test/image")
	if err != nil || len(tags) != 2 || calls != 2 {
		t.Fatal(tags, err, calls)
	}
}
func TestRegistryFailsClosed(t *testing.T) {
	for _, body := range []string{`{}`, `broken`, `{"results":null}`, `{"results":[{}]}`, `{"results":[],"next":"https://other.test/tags"}`, `{"results":[],"next":"?page_size=100"}`} {
		t.Run(body, func(t *testing.T) {
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
			defer s.Close()
			if tags, e := Tags(context.Background(), s.Client(), s.URL, "test/image"); e == nil {
				t.Fatal("partial listing accepted", tags)
			}
		})
	}
}
func TestRegistryHTTPFailure(t *testing.T) {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503) }))
	defer s.Close()
	if _, e := Tags(context.Background(), s.Client(), s.URL, "test/image"); e == nil {
		t.Fatal("HTTP failure accepted")
	}
}

func TestRepositoryVersionCanBePublished(t *testing.T) {
	major, err := Major("../../VERSION")
	if err != nil || major < 1 {
		t.Fatal("invalid release VERSION", major, err)
	}
}
