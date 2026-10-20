package main

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/jackc/pgx/v5/pgxpool"
	sdk "github.com/modelcontextprotocol/go-sdk/mcp"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"supportbot/internal/config"
	"sync/atomic"
	"testing"
	"time"
)

func TestLocalStartupHealthCommandsAndShutdownWithRealDB(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		dsn = os.Getenv("SUPPORTBOT_TEST_DATABASE_URL")
	}
	if dsn == "" {
		t.Skip("requires isolated Docker PostgreSQL")
	}
	t.Chdir("../..")
	ctx := context.Background()
	pool, e := pgxpool.New(ctx, dsn)
	if e != nil {
		t.Fatal(e)
	}
	defer pool.Close()
	schema := fmt.Sprintf("runtime_parity_%d", time.Now().UnixNano())
	if _, e = pool.Exec(ctx, "CREATE SCHEMA "+schema); e != nil {
		t.Fatal(e)
	}
	defer pool.Exec(ctx, "DROP SCHEMA "+schema+" CASCADE")
	parsed, _ := url.Parse(dsn)
	q := parsed.Query()
	q.Set("search_path", schema+",public")
	parsed.RawQuery = q.Encode()
	mcpServer := sdk.NewServer(&sdk.Implementation{Name: "local-fixture", Version: "1"}, nil)
	mcpHandler := sdk.NewStreamableHTTPHandler(func(*http.Request) *sdk.Server { return mcpServer }, &sdk.StreamableHTTPOptions{JSONResponse: true, DisableLocalhostProtection: true})
	var updates atomic.Int64
	var commands atomic.Bool
	help := make(chan struct{}, 1)
	mux := http.NewServeMux()
	mux.Handle("/mcp", mcpHandler)
	mux.HandleFunc("/embeddings", func(w http.ResponseWriter, r *http.Request) {
		var p struct {
			Input json.RawMessage `json:"input"`
		}
		json.NewDecoder(r.Body).Decode(&p)
		var texts []string
		if json.Unmarshal(p.Input, &texts) != nil {
			texts = []string{"one"}
		}
		vector := make([]float64, 1536)
		vector[0] = 1
		rows := []any{}
		for i := range texts {
			rows = append(rows, map[string]any{"embedding": vector, "index": i})
		}
		json.NewEncoder(w).Encode(map[string]any{"data": rows})
	})
	mux.HandleFunc("/bottest/", func(w http.ResponseWriter, r *http.Request) {
		method := strings.TrimPrefix(r.URL.Path, "/bottest/")
		switch method {
		case "getUpdates":
			if updates.Add(1) == 1 {
				io.WriteString(w, `{"ok":true,"result":[{"update_id":1,"message":{"message_id":1,"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"/help"}}]}`)
			} else {
				select {
				case <-r.Context().Done():
					return
				case <-time.After(20 * time.Millisecond):
					io.WriteString(w, `{"ok":true,"result":[]}`)
				}
			}
		case "setMyCommands":
			var p struct {
				Commands []struct{ Command, Description string }
			}
			json.NewDecoder(r.Body).Decode(&p)
			if len(p.Commands) == 3 && p.Commands[0].Description == "Начать заново, сбросить историю" && p.Commands[1].Description == "Связаться с живым оператором" {
				commands.Store(true)
			}
			io.WriteString(w, `{"ok":true,"result":true}`)
		case "sendMessage":
			var p map[string]any
			json.NewDecoder(r.Body).Decode(&p)
			if text, _ := p["text"].(string); strings.Contains(text, "Я бот технической поддержки VPN-сервиса") {
				select {
				case help <- struct{}{}:
				default:
				}
			}
			io.WriteString(w, `{"ok":true,"result":{"message_id":2}}`)
		default:
			io.WriteString(w, `{"ok":true,"result":true}`)
		}
	})
	server := httptest.NewServer(mux)
	defer server.Close()
	settings := config.Default()
	settings.TelegramBotToken = "test"
	settings.TelegramSupportGroupChatID = -100
	settings.OpenAIAPIKey = "sk-local-only"
	settings.EmbeddingProvider = "openai"
	settings.OpenAIBaseURL = server.URL
	settings.RemnawaveMCPURL = server.URL + "/mcp"
	ready := make(chan string, 1)
	runCtx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- run(runCtx, settings, runOptions{HTTP: server.Client(), DatabaseURL: parsed.String(), TelegramBaseURL: server.URL, ListenAddress: "127.0.0.1:0", Ready: ready})
	}()
	var address string
	select {
	case address = <-ready:
	case e := <-done:
		t.Fatalf("startup failed: %v", e)
	case <-time.After(15 * time.Second):
		cancel()
		t.Fatal("startup timeout")
	}
	for _, path := range []string{"/health", "/actuator/health"} {
		response, e := server.Client().Get("http://" + address + path)
		if e != nil {
			cancel()
			t.Fatal(e)
		}
		var body map[string]string
		json.NewDecoder(response.Body).Decode(&body)
		response.Body.Close()
		if response.StatusCode != 200 || body["status"] != "UP" {
			t.Fatalf("bad health %v", body)
		}
	}
	select {
	case <-help:
	case <-time.After(3 * time.Second):
		cancel()
		t.Fatal("Telegram update was not routed")
	}
	if !commands.Load() {
		t.Fatal("bot commands differ from Python")
	}
	cancel()
	select {
	case e := <-done:
		if e != nil {
			t.Fatal(e)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("shutdown hung")
	}
	if _, e = server.Client().Get("http://" + address + "/health"); e == nil {
		t.Fatal("health server survived shutdown")
	}
	var rows int
	if e = pool.QueryRow(ctx, "SELECT count(*) FROM "+schema+".faq WHERE embedding IS NOT NULL").Scan(&rows); e != nil || rows < 1 {
		t.Fatalf("real FAQ index absent: rows=%d error=%v", rows, e)
	}
}
func TestInvalidSettingsStopBeforeNetwork(t *testing.T) {
	called := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { called = true }))
	defer server.Close()
	s := config.Default()
	e := run(context.Background(), s, runOptions{TelegramBaseURL: server.URL, DatabaseURL: "postgres://invalid"})
	if e == nil || called {
		t.Fatal("invalid config reached network")
	}
}
