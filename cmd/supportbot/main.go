package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"supportbot/internal/bedolaga"
	"supportbot/internal/bot"
	"supportbot/internal/config"
	"supportbot/internal/diagnostics"
	"supportbot/internal/domain"
	"supportbot/internal/llm"
	"supportbot/internal/logging"
	"supportbot/internal/mcp"
	"supportbot/internal/rag"
	"supportbot/internal/storage"
	"supportbot/internal/telegram"
	"sync"
	"syscall"
	"time"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		port := os.Getenv("HEALTHCHECK_PORT")
		if port == "" {
			port = "8080"
		}
		h := http.Client{Timeout: 5 * time.Second}
		r, e := h.Get("http://127.0.0.1:" + port + "/health")
		if e != nil {
			os.Exit(1)
		}
		defer r.Body.Close()
		if r.StatusCode != 200 {
			os.Exit(1)
		}
		return
	}
	settings, e := config.Load()
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
	logging.Setup(settings)
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	if e = run(ctx, settings, runOptions{}); e != nil {
		slog.Error("bot stopped", "error", e)
		os.Exit(1)
	}
}

type runOptions struct {
	HTTP            *http.Client
	TelegramBaseURL string
	DatabaseURL     string
	ListenAddress   string
	Ready           chan<- string
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]string{"status": "UP"})
}
func run(ctx context.Context, s config.Settings, options runOptions) error {
	diagnostics.RegisterSettings(s)
	ctx = diagnostics.WithOperation(ctx, "runtime", "startup")
	diagnostics.Info(ctx, "Starting support bot")
	if e := s.Validate(); e != nil {
		return e
	}
	httpClient := options.HTTP
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 150 * time.Second}
	}
	defer httpClient.CloseIdleConnections()
	dsn := options.DatabaseURL
	if dsn == "" {
		dsn = s.DatabaseURL()
	}
	store, e := storage.New(ctx, dsn)
	if e != nil {
		return e
	}
	defer store.Close()
	embedding := rag.NewEmbedding(s, httpClient)
	if e = store.Migrate(ctx, embedding.Dimension()); e != nil {
		return e
	}
	faq := rag.New(store, embedding)
	gaps := rag.NewGaps(store, faq, embedding)
	history := storage.NewHistory(store, s.ChatHistoryMaxMessages, s.ChatHistoryTTLDays)
	api := telegram.New(s.TelegramBotToken, httpClient)
	if options.TelegramBaseURL != "" {
		api.BaseURL = options.TelegramBaseURL
	} else if s.TelegramAPIBaseURL != "" {
		api.BaseURL = s.TelegramAPIBaseURL
	}
	sender := telegram.NewSender(api)
	sender.HTTP = httpClient
	notifier := &bot.Notifier{API: api, Group: s.TelegramSupportGroupChatID}
	notify := func(c context.Context, label string, e error) { notifier.Notify(c, label, 0, e) }
	clients := []*mcp.HTTPClient{mcp.NewHTTPClient("remnawave", s.RemnawaveMCPURL, httpClient, notify)}
	if s.BedolagaMCPEnabled {
		clients = append(clients, mcp.NewHTTPClient("bedolaga", s.BedolagaMCPURL, httpClient, notify))
	}
	defer func() {
		for _, client := range clients {
			_ = client.Close()
		}
	}()
	generic := []mcp.Client{}
	for _, client := range clients {
		if !client.Init(ctx) {
			notifier.Notify(ctx, "Бот запущен БЕЗ инструментов "+client.Name+": не удалось инициализировать MCP ("+client.BaseURL+"). Проверьте доступность и логи MCP-контейнера.", 0, fmt.Errorf("MCP[%s] initialization failed", client.Name))
		}
		generic = append(generic, client)
	}
	tools := mcp.NewRouter(generic, s.RemnawaveMCPReadonly)
	collisions := tools.Collisions()
	names := make([]string, 0, len(collisions))
	for name := range collisions {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		notifier.Notify(ctx, "Одноимённый инструмент MCP '"+name+"' объявлен несколькими серверами ("+strings.Join(collisions[name], ", ")+"). Имя скрыто от модели. Переименуйте инструмент в одном из MCP-серверов.", 0, fmt.Errorf("MCP tool name collision: %s", name))
	}
	for _, client := range clients {
		if client.Initialized() && tools.ToolCount(client.Name) == 0 {
			notifier.Notify(ctx, "Бот запущен БЕЗ инструментов "+client.Name+": MCP вернул 0 разрешённых инструментов. Проверьте MCP_TAG, REMNAWAVE_IS_SUPPORT и allowlist бота ("+client.BaseURL+").", 0, fmt.Errorf("MCP[%s] exposed no allowed tools", client.Name))
		}
	}
	model, e := llm.New(s, tools, history, faq, store, httpClient)
	if e != nil {
		return e
	}
	state := bot.NewConversationState(time.Duration(s.ConversationOperatorSuppressionWindowMinutes)*time.Minute, time.Duration(s.ConversationLastQueryTTLHours)*time.Hour)
	rate := bot.NewRateLimiter()
	forward := &bot.Forwarder{Sender: sender, Store: store, Group: s.TelegramSupportGroupChatID, Admin: s.TelegramSupportAdminUsername}
	pipeline := &bot.Pipeline{LLM: model, Sender: sender, Forwarder: forward, Rate: rate, Gaps: gaps, State: state}
	work, cancelWork := context.WithCancel(context.Background())
	defer cancelWork()
	buffer := bot.NewBuffer(work, time.Duration(s.TelegramBufferWindowMS)*time.Millisecond, s.TelegramBufferMaxMessages, pipeline.Handle)
	router := &bot.Router{Sender: sender, LLM: model, Store: store, History: history, Gaps: gaps, State: state, Forwarder: forward, Buffer: buffer, Pipeline: pipeline, Group: s.TelegramSupportGroupChatID, Admins: s.TelegramSupportAdminTelegramIDs}
	var answerer *bedolaga.Answerer
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", healthHandler)
	mux.HandleFunc("GET /actuator/health", healthHandler)
	if s.BedolagaEnabled {
		panel := bedolaga.NewClient(s.BedolagaAPIURL, s.BedolagaAPIKey, httpClient)
		answerer = bedolaga.NewAnswerer(work, panel, model, store, rate, notifier, forward, gaps, state, s.BedolagaMaxConcurrentTickets)
		router.Relay = &bedolaga.Relay{Client: panel, Store: store, State: state}
		mux.HandleFunc(s.BedolagaWebhookPath, bedolaga.Webhook(answerer, s.BedolagaWebhookSecret))
	}
	if e = faq.Initialize(ctx, "faq/faq.json"); e != nil {
		slog.Error("FAQ initialization failed; continuing with available retrieval", "error", e)
	}
	retryCtx, cancelRetry := context.WithCancel(work)
	var retries sync.WaitGroup
	for _, client := range clients {
		c := client
		retries.Add(1)
		go func() { defer retries.Done(); c.RetryConnection(retryCtx) }()
	}
	defer func() { cancelRetry(); retries.Wait() }()
	jobs := []bot.MaintenanceJob{{Name: "chat-history-eviction", Interval: time.Hour, Run: history.EvictStaleEntries}, {Name: "rate-limiter-eviction", Interval: 10 * time.Minute, Run: func(context.Context) error { rate.Evict(); return nil }}, {Name: "conversation-state-eviction", Interval: 15 * time.Minute, Run: func(context.Context) error { state.Evict(); return nil }}}
	if answerer != nil {
		jobs = append(jobs, bot.MaintenanceJob{Name: "bedolaga-ticket-sweep", Interval: time.Duration(s.BedolagaPollIntervalSeconds) * time.Second, Run: func(c context.Context) error { answerer.Sweep(c, 50); return nil }})
	}
	maintenance := bot.StartMaintenance(work, jobs)
	address := options.ListenAddress
	if address == "" {
		address = ":" + strconv.Itoa(s.HealthcheckPort)
	}
	listener, e := net.Listen("tcp", address)
	if e != nil {
		maintenance.Stop()
		buffer.Shutdown()
		return e
	}
	server := &http.Server{Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	serverErrors := make(chan error, 1)
	go func() { serverErrors <- server.Serve(listener) }()
	diagnostics.Info(ctx, "Health server started", "port", s.HealthcheckPort)
	if options.Ready != nil {
		options.Ready <- listener.Addr().String()
	}
	e = api.Call(ctx, "setMyCommands", map[string]any{"commands": []map[string]string{{"command": "start", "description": "Начать заново, сбросить историю"}, {"command": "operator", "description": "Связаться с живым оператором"}, {"command": "help", "description": "Что умеет бот"}}}, nil)
	if e != nil {
		diagnostics.Error(ctx, "Bot command registration failed", e)
	} else {
		diagnostics.Info(ctx, "Bot commands registered")
	}
	pollCtx, cancelPoll := context.WithCancel(ctx)
	defer cancelPoll()
	pollDone := make(chan error, 1)
	go func() { pollDone <- poll(pollCtx, work, api, router) }()
	var outcome error
	pollReturned := false
	select {
	case <-ctx.Done():
	case outcome = <-pollDone:
		pollReturned = true
	case outcome = <-serverErrors:
		if errors.Is(outcome, http.ErrServerClosed) {
			outcome = nil
		}
	}
	diagnostics.Info(ctx, "Stopping support bot")
	cancelPoll()
	if !pollReturned {
		select {
		case <-pollDone:
		case <-time.After(5 * time.Second):
			slog.Warn("Telegram intake did not drain within shutdown bound")
		}
	}
	maintenance.Stop()
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	_ = server.Shutdown(shutdownCtx)
	shutdownCancel()
	drainCtx, drainCancel := context.WithTimeout(context.Background(), 20*time.Second)
	buffer.Drain(drainCtx)
	drainCancel()
	buffer.Shutdown()
	if answerer != nil {
		ticketCtx, ticketCancel := context.WithTimeout(context.Background(), 3*time.Second)
		answerer.Drain(ticketCtx)
		ticketCancel()
	}
	cancelWork()
	diagnostics.Info(ctx, "Support bot shutdown completed")
	if errors.Is(outcome, context.Canceled) {
		return nil
	}
	return outcome
}
func poll(ctx, work context.Context, api telegram.API, router *bot.Router) error {
	offset := int64(0)
	var inflight sync.WaitGroup
	defer inflight.Wait()
	for {
		if ctx.Err() != nil {
			return nil
		}
		var updates []telegram.Update
		e := api.Call(ctx, "getUpdates", map[string]any{"offset": offset, "timeout": 30, "allowed_updates": []string{"message", "message_reaction", "callback_query"}}, &updates)
		if e != nil {
			if ctx.Err() != nil {
				return nil
			}
			slog.Error("Telegram polling request failed")
			select {
			case <-ctx.Done():
				return nil
			case <-time.After(time.Second):
			}
			continue
		}
		for _, u := range updates {
			if u.ID >= offset {
				offset = u.ID + 1
			}
			inflight.Add(1)
			go func(update telegram.Update) { defer inflight.Done(); router.Handle(work, update) }(u)
		}
	}
}

var _ domain.LLM
