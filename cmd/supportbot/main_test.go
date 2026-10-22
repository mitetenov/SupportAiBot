package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"supportbot/internal/config"
	"testing"
)

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
