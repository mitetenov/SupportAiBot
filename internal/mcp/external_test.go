package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"reflect"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// Optional acceptance against the actual sibling MCP binaries. Both backends are
// local fixtures; no production configuration or credentials are inherited.
func TestExternalMCPBinaryContracts(t *testing.T) {
	for _, tc := range []struct {
		name, env, tool string
		args            map[string]any
		expected        string
	}{{"remnawave", "TEST_REMNAWAVE_MCP_BIN", "nodes_list", nil, "Fixture node"}, {"bedolaga", "TEST_BEDOLAGA_MCP_BIN", "bedolaga_user_get", map[string]any{"telegram_id": 42}, `"ok":true`}} {
		t.Run(tc.name, func(t *testing.T) {
			binary := os.Getenv(tc.env)
			if binary == "" {
				t.Skip(tc.env + " required for sibling binary acceptance")
			}
			var upstreamCalls atomic.Int32
			backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				upstreamCalls.Add(1)
				w.Header().Set("Content-Type", "application/json")
				if tc.name == "remnawave" {
					if r.Header.Get("Authorization") != "Bearer fixture-token" {
						t.Error("Remnawave API authentication mismatch")
					}
					io.WriteString(w, `{"response":[{"uuid":"fixture-node","name":"Fixture node"}]}`)
				} else {
					if r.Header.Get("X-API-Key") != "fixture-key" {
						t.Error("Bedolaga API authentication mismatch")
					}
					if r.URL.Path != "/users/by-telegram-id/42" {
						t.Errorf("pinned identity lookup wrong %s", r.URL.Path)
					}
					io.WriteString(w, `{"id":17,"telegram_id":42,"status":"active","balance_kopeks":1234}`)
				}
			}))
			defer backend.Close()
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			port := listener.Addr().(*net.TCPAddr).Port
			listener.Close()
			url := "http://127.0.0.1:" + strconv.Itoa(port)
			process := exec.Command(binary, "http")
			process.Env = []string{"MCP_HTTP_HOST=127.0.0.1", "MCP_HTTP_PORT=" + strconv.Itoa(port), "REMNAWAVE_BASE_URL=" + backend.URL, "REMNAWAVE_API_TOKEN=fixture-token", "REMNAWAVE_IS_SUPPORT=true", "BEDOLAGA_API_URL=" + backend.URL, "BEDOLAGA_API_KEY=fixture-key"}
			var logs bytes.Buffer
			process.Stderr = &logs
			process.Stdout = &logs
			if err = process.Start(); err != nil {
				t.Fatal(err)
			}
			defer func() { process.Process.Kill(); process.Wait() }()
			httpClient := &http.Client{Timeout: time.Second}
			ready := false
			for until := time.Now().Add(5 * time.Second); time.Now().Before(until); {
				r, e := httpClient.Get(url + "/health")
				if e == nil {
					r.Body.Close()
					if r.StatusCode == 200 {
						ready = true
						break
					}
				}
				time.Sleep(20 * time.Millisecond)
			}
			if !ready {
				t.Fatalf("sibling MCP did not start: %s", logs.String())
			}
			client := NewHTTPClient(tc.name, url, httpClient, nil)
			if !client.Init(context.Background()) {
				t.Fatalf("sibling native SDK initialization failed: %s", logs.String())
			}
			defer client.Close()
			var expected []Tool
			raw, err := os.ReadFile("testdata/" + tc.name + "_tools.json")
			if err != nil {
				t.Fatal(err)
			}
			if json.Unmarshal(raw, &expected) != nil {
				t.Fatal("invalid tool snapshot")
			}
			actual := client.ListTools()
			byName := map[string]Tool{}
			for _, tool := range actual {
				byName[tool.Name] = tool
			}
			if len(actual) != len(expected) {
				t.Fatalf("tool count changed actual=%d snapshot=%d", len(actual), len(expected))
			}
			for _, want := range expected {
				got, ok := byName[want.Name]
				if !ok || !reflect.DeepEqual(got, want) {
					t.Fatalf("tool declaration differs %s\ngot %s\nwant %s", want.Name, jsonString(got), jsonString(want))
				}
			}
			router := NewRouter([]Client{client}, false)
			expectedCount := 8
			if tc.name == "remnawave" {
				expectedCount = 10
			}
			if router.ToolCount(tc.name) != expectedCount {
				t.Fatal("profile count changed")
			}
			result, err := router.CallTool(context.Background(), tc.tool, tc.args, 42)
			if err != nil || !strings.Contains(result, tc.expected) {
				t.Fatalf("sibling tool failed result=%s err=%v", result, err)
			}
			if upstreamCalls.Load() != 1 {
				t.Fatalf("unexpected backend calls %d", upstreamCalls.Load())
			}
		})
	}
}
