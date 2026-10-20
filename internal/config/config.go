// Package config preserves the Python environment configuration contract.
package config

import (
	"fmt"
	"github.com/joho/godotenv"
	"math"
	"net/url"
	"os"
	"reflect"
	"strconv"
	"strings"
)

type ProviderTarget struct {
	Provider string
	Model    string
}
type Settings struct {
	TelegramAPIBaseURL                           string           `env:"TELEGRAM_API_BASE_URL" json:"-"`
	BotLogLevel                                  string           `env:"BOT_LOG_LEVEL" json:"-"`
	PgvectorHost                                 string           `env:"PGVECTOR_HOST" json:"-"`
	PgvectorPort                                 int              `env:"PGVECTOR_PORT" json:"-"`
	PgvectorDB                                   string           `env:"PGVECTOR_DB" json:"-"`
	PgvectorUser                                 string           `env:"PGVECTOR_USER" json:"-"`
	PgvectorPassword                             string           `env:"PGVECTOR_PASSWORD" json:"-"`
	ChatHistoryMaxMessages                       int              `env:"CHAT_HISTORY_MAX_MESSAGES" json:"-"`
	ChatHistoryTTLDays                           int              `env:"CHAT_HISTORY_TTL_DAYS" json:"-"`
	ConversationOperatorSuppressionWindowMinutes int              `env:"CONVERSATION_OPERATOR_SUPPRESSION_WINDOW_MINUTES" json:"-"`
	ConversationLastQueryTTLHours                int              `env:"CONVERSATION_LAST_QUERY_TTL_HOURS" json:"-"`
	LLMProvider                                  string           `env:"LLM_PROVIDER" json:"-"`
	LLMFallbackChain                             []ProviderTarget `env:"LLM_FALLBACK_CHAIN" json:"-"`
	EmbeddingProvider                            string           `env:"EMBEDDING_PROVIDER" json:"-"`
	ReasoningEffort                              string           `env:"REASONING_EFFORT" json:"-"`
	TelegramBotToken                             string           `env:"TELEGRAM_BOT_TOKEN" json:"-"`
	TelegramSupportGroupChatID                   int64            `env:"TELEGRAM_SUPPORT_GROUP_CHAT_ID" json:"-"`
	TelegramSupportAdminUsername                 string           `env:"TELEGRAM_SUPPORT_ADMIN_USERNAME" json:"-"`
	TelegramSupportAdminTelegramIDs              map[int64]bool   `env:"TELEGRAM_SUPPORT_ADMIN_TELEGRAM_IDS" json:"-"`
	TelegramBufferWindowMS                       int              `env:"TELEGRAM_BUFFER_WINDOW_MS" json:"-"`
	TelegramBufferMaxMessages                    int              `env:"TELEGRAM_BUFFER_MAX_MESSAGES" json:"-"`
	DeepSeekAPIKey                               string           `env:"DEEPSEEK_API_KEY" json:"-"`
	DeepSeekBaseURL                              string           `env:"DEEPSEEK_BASE_URL" json:"-"`
	DeepSeekModel                                string           `env:"DEEPSEEK_MODEL" json:"-"`
	GeminiAPIKey                                 string           `env:"GEMINI_API_KEY" json:"-"`
	GeminiBaseURL                                string           `env:"GEMINI_BASE_URL" json:"-"`
	GeminiModel                                  string           `env:"GEMINI_MODEL" json:"-"`
	OpenAIAPIKey                                 string           `env:"OPENAI_API_KEY" json:"-"`
	OpenAIBaseURL                                string           `env:"OPENAI_BASE_URL" json:"-"`
	OpenAIModel                                  string           `env:"OPENAI_MODEL" json:"-"`
	OpenAIEmbeddingModel                         string           `env:"OPENAI_EMBEDDING_MODEL" json:"-"`
	OpenAITemperature                            *float64         `env:"OPENAI_TEMPERATURE" json:"-"`
	GroqAPIKey                                   string           `env:"GROQ_API_KEY" json:"-"`
	GroqBaseURL                                  string           `env:"GROQ_BASE_URL" json:"-"`
	GroqModel                                    string           `env:"GROQ_MODEL" json:"-"`
	OpenRouterAPIKey                             string           `env:"OPENROUTER_API_KEY" json:"-"`
	OpenRouterBaseURL                            string           `env:"OPENROUTER_BASE_URL" json:"-"`
	OpenRouterModel                              string           `env:"OPENROUTER_MODEL" json:"-"`
	OpenRouterTimeoutSeconds                     float64          `env:"OPENROUTER_TIMEOUT_SECONDS" json:"-"`
	ZAIAPIKey                                    string           `env:"ZAI_API_KEY" json:"-"`
	ZAIBaseURL                                   string           `env:"ZAI_BASE_URL" json:"-"`
	ZAIModel                                     string           `env:"ZAI_MODEL" json:"-"`
	ZAITimeoutSeconds                            float64          `env:"ZAI_TIMEOUT_SECONDS" json:"-"`
	RemnawaveMCPURL                              string           `env:"REMNAWAVE_MCP_URL" json:"-"`
	RemnawaveBaseURL                             string           `env:"REMNAWAVE_BASE_URL" json:"-"`
	RemnawaveAPIToken                            string           `env:"REMNAWAVE_API_TOKEN" json:"-"`
	RemnawaveMCPReadonly                         bool             `env:"REMNAWAVE_MCP_READONLY" json:"-"`
	BedolagaMCPEnabled                           bool             `env:"BEDOLAGA_MCP_ENABLED" json:"-"`
	BedolagaMCPURL                               string           `env:"BEDOLAGA_MCP_URL" json:"-"`
	BedolagaEnabled                              bool             `env:"BEDOLAGA_ENABLED" json:"-"`
	BedolagaAPIURL                               string           `env:"BEDOLAGA_API_URL" json:"-"`
	BedolagaAPIKey                               string           `env:"BEDOLAGA_API_KEY" json:"-"`
	BedolagaWebhookSecret                        string           `env:"BEDOLAGA_WEBHOOK_SECRET" json:"-"`
	BedolagaWebhookPath                          string           `env:"BEDOLAGA_WEBHOOK_PATH" json:"-"`
	BedolagaPollIntervalSeconds                  int              `env:"BEDOLAGA_POLL_INTERVAL_SECONDS" json:"-"`
	BedolagaMaxConcurrentTickets                 int              `env:"BEDOLAGA_MAX_CONCURRENT_TICKETS" json:"-"`
	HealthcheckPort                              int              `env:"HEALTHCHECK_PORT" json:"-"`
}

func Default() Settings {
	return Settings{
		BotLogLevel:            "INFO",
		PgvectorHost:           "pgvector",
		PgvectorPort:           5432,
		PgvectorDB:             "vpnsupport",
		PgvectorUser:           "bot",
		PgvectorPassword:       "",
		ChatHistoryMaxMessages: 20,
		ChatHistoryTTLDays:     7,
		ConversationOperatorSuppressionWindowMinutes: 30,
		ConversationLastQueryTTLHours:                6,
		LLMProvider:                                  "openai",
		LLMFallbackChain:                             nil,
		EmbeddingProvider:                            "gemini",
		ReasoningEffort:                              "none",
		TelegramBotToken:                             "",
		TelegramSupportGroupChatID:                   0,
		TelegramSupportAdminUsername:                 "",
		TelegramSupportAdminTelegramIDs:              map[int64]bool{},
		TelegramBufferWindowMS:                       2500,
		TelegramBufferMaxMessages:                    5,
		DeepSeekAPIKey:                               "",
		DeepSeekBaseURL:                              "https://api.deepseek.com/v1",
		DeepSeekModel:                                "",
		GeminiAPIKey:                                 "",
		GeminiBaseURL:                                "https://generativelanguage.googleapis.com/v1beta",
		GeminiModel:                                  "",
		OpenAIAPIKey:                                 "",
		OpenAIBaseURL:                                "https://api.openai.com/v1",
		OpenAIModel:                                  "gpt-5.6-luna",
		OpenAIEmbeddingModel:                         "text-embedding-3-small",
		OpenAITemperature:                            nil,
		GroqAPIKey:                                   "",
		GroqBaseURL:                                  "https://api.groq.com/openai/v1",
		GroqModel:                                    "llama-3.3-70b-versatile",
		OpenRouterAPIKey:                             "",
		OpenRouterBaseURL:                            "https://openrouter.ai/api/v1",
		OpenRouterModel:                              "",
		OpenRouterTimeoutSeconds:                     120.0,
		ZAIAPIKey:                                    "",
		ZAIBaseURL:                                   "https://api.z.ai/api/paas/v4",
		ZAIModel:                                     "",
		ZAITimeoutSeconds:                            120.0,
		RemnawaveMCPURL:                              "http://localhost:3100",
		RemnawaveBaseURL:                             "",
		RemnawaveAPIToken:                            "",
		RemnawaveMCPReadonly:                         false,
		BedolagaMCPEnabled:                           false,
		BedolagaMCPURL:                               "",
		BedolagaEnabled:                              false,
		BedolagaAPIURL:                               "",
		BedolagaAPIKey:                               "",
		BedolagaWebhookSecret:                        "",
		BedolagaWebhookPath:                          "/bedolaga/webhook",
		BedolagaPollIntervalSeconds:                  60,
		BedolagaMaxConcurrentTickets:                 5,
		HealthcheckPort:                              8080,
	}
}
func Load() (Settings, error) {
	_ = godotenv.Load()
	env := map[string]string{}
	for _, item := range os.Environ() {
		key, value, ok := strings.Cut(item, "=")
		if ok {
			env[strings.ToUpper(key)] = value
		}
	}
	s, e := FromEnv(func(key string) (string, bool) { value, ok := env[key]; return value, ok })
	if e != nil {
		return s, e
	}
	return s, s.Validate()
}
func FromEnv(lookup func(string) (string, bool)) (Settings, error) {
	s := Default()
	v := reflect.ValueOf(&s).Elem()
	t := v.Type()
	for i := 0; i < v.NumField(); i++ {
		raw, ok := lookup(t.Field(i).Tag.Get("env"))
		if !ok {
			continue
		}
		f := v.Field(i)
		switch f.Kind() {
		case reflect.String:
			f.SetString(raw)
		case reflect.Bool:
			normalized := strings.ToLower(strings.TrimSpace(raw))
			if normalized == "yes" || normalized == "on" || normalized == "y" || normalized == "t" {
				normalized = "true"
			}
			if normalized == "no" || normalized == "off" || normalized == "n" || normalized == "f" {
				normalized = "false"
			}
			x, e := strconv.ParseBool(normalized)
			if e != nil {
				return s, fmt.Errorf("%s must be a boolean", t.Field(i).Tag.Get("env"))
			}
			f.SetBool(x)
		case reflect.Int, reflect.Int64:
			x, e := strconv.ParseInt(strings.TrimSpace(raw), 10, 64)
			if e != nil {
				return s, fmt.Errorf("%s must be an integer", t.Field(i).Tag.Get("env"))
			}
			f.SetInt(x)
		case reflect.Float64:
			x, e := strconv.ParseFloat(raw, 64)
			if e != nil {
				return s, fmt.Errorf("%s must be a number", t.Field(i).Tag.Get("env"))
			}
			f.SetFloat(x)
		case reflect.Pointer:
			if strings.TrimSpace(raw) == "" {
				return s, fmt.Errorf("%s must be a number", t.Field(i).Tag.Get("env"))
			}
			if strings.TrimSpace(raw) != "" {
				x, e := strconv.ParseFloat(raw, 64)
				if e != nil {
					return s, fmt.Errorf("%s must be a number", t.Field(i).Tag.Get("env"))
				}
				f.Set(reflect.ValueOf(&x))
			}
		case reflect.Map:
			m := map[int64]bool{}
			for _, x := range strings.Split(raw, ",") {
				if strings.TrimSpace(x) == "" {
					continue
				}
				id, e := strconv.ParseInt(strings.TrimSpace(x), 10, 64)
				if e != nil {
					continue
				}
				m[id] = true
			}
			f.Set(reflect.ValueOf(m))
		case reflect.Slice:
			chain, e := ParseFallbackChain(raw)
			if e != nil {
				return s, e
			}
			f.Set(reflect.ValueOf(chain))
		}
	}
	if _, ok := lookup("BOT_LOG_LEVEL"); !ok {
		if x, ok := lookup("LOG_LEVEL"); ok {
			s.BotLogLevel = x
		}
	}
	s.BotLogLevel = strings.ToUpper(strings.TrimSpace(s.BotLogLevel))
	s.LLMProvider = strings.ToLower(strings.TrimSpace(s.LLMProvider))
	s.EmbeddingProvider = strings.ToLower(strings.TrimSpace(s.EmbeddingProvider))
	s.ReasoningEffort = strings.ToLower(strings.TrimSpace(s.ReasoningEffort))
	return s, nil
}
func ParseFallbackChain(raw string) ([]ProviderTarget, error) {
	out := []ProviderTarget{}
	if raw == "" {
		return out, nil
	}
	for _, item := range strings.Split(raw, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			return nil, fmt.Errorf("LLM_FALLBACK_CHAIN must not contain empty elements")
		}
		p, m, ok := strings.Cut(item, ":")
		p = strings.ToLower(strings.TrimSpace(p))
		m = strings.TrimSpace(m)
		if !ok || !validProvider(p) || m == "" {
			return nil, fmt.Errorf("LLM_FALLBACK_CHAIN must contain provider:model targets")
		}
		out = append(out, ProviderTarget{p, m})
	}
	return out, nil
}
func validProvider(p string) bool {
	switch p {
	case "openai", "gemini", "deepseek", "groq", "openrouter", "zai":
		return true
	}
	return false
}
func (s Settings) Model(provider string) string {
	switch provider {
	case "openai":
		return s.OpenAIModel
	case "gemini":
		return s.GeminiModel
	case "deepseek":
		return s.DeepSeekModel
	case "groq":
		return s.GroqModel
	case "openrouter":
		return s.OpenRouterModel
	case "zai":
		return s.ZAIModel
	}
	return ""
}
func (s Settings) APIKey(provider string) string {
	switch provider {
	case "openai":
		return s.OpenAIAPIKey
	case "gemini":
		return s.GeminiAPIKey
	case "deepseek":
		return s.DeepSeekAPIKey
	case "groq":
		return s.GroqAPIKey
	case "openrouter":
		return s.OpenRouterAPIKey
	case "zai":
		return s.ZAIAPIKey
	}
	return ""
}
func (s Settings) LLMProviderTargets() []ProviderTarget {
	return append([]ProviderTarget{{s.LLMProvider, s.Model(s.LLMProvider)}}, s.LLMFallbackChain...)
}
func (s Settings) DatabaseURL() string {
	u := url.URL{Scheme: "postgres", Host: fmt.Sprintf("%s:%d", s.PgvectorHost, s.PgvectorPort), Path: s.PgvectorDB, User: url.UserPassword(s.PgvectorUser, s.PgvectorPassword)}
	return u.String()
}
func (s Settings) Validate() error {
	if strings.TrimSpace(s.TelegramBotToken) == "" {
		return fmt.Errorf("TELEGRAM_BOT_TOKEN is required")
	}
	if s.TelegramSupportGroupChatID >= 0 {
		return fmt.Errorf("TELEGRAM_SUPPORT_GROUP_CHAT_ID must be negative")
	}
	if s.BotLogLevel != "TRACE" && s.BotLogLevel != "INFO" && s.BotLogLevel != "ERROR" {
		return fmt.Errorf("BOT_LOG_LEVEL must be TRACE, INFO, or ERROR")
	}
	switch s.ReasoningEffort {
	case "none", "minimal", "low", "medium", "high", "xhigh", "max":
	default:
		return fmt.Errorf("unsupported REASONING_EFFORT")
	}
	for _, t := range s.LLMProviderTargets() {
		if !validProvider(t.Provider) {
			return fmt.Errorf("unknown LLM provider")
		}
		if strings.TrimSpace(s.APIKey(t.Provider)) == "" || strings.TrimSpace(t.Model) == "" {
			return fmt.Errorf("%s API key and model are required", strings.ToUpper(t.Provider))
		}
		if t.Provider == "openai" && !strings.HasPrefix(strings.TrimSpace(s.OpenAIAPIKey), "sk-") {
			return fmt.Errorf("OPENAI_API_KEY must begin with sk-")
		}
		if t.Provider == "openrouter" || t.Provider == "zai" {
			base, timeout := s.OpenRouterBaseURL, s.OpenRouterTimeoutSeconds
			if t.Provider == "zai" {
				base, timeout = s.ZAIBaseURL, s.ZAITimeoutSeconds
			}
			u, e := url.Parse(base)
			if e != nil || u.Hostname() == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" || strings.HasSuffix(strings.TrimRight(u.Path, "/"), "/chat/completions") {
				return fmt.Errorf("%s_BASE_URL must be a base HTTP URL without credentials, query, fragment or chat endpoint", strings.ToUpper(t.Provider))
			}
			if timeout <= 0 || math.IsNaN(timeout) || math.IsInf(timeout, 0) {
				return fmt.Errorf("%s_TIMEOUT_SECONDS must be positive", strings.ToUpper(t.Provider))
			}
		}
	}
	if s.EmbeddingProvider != "openai" && s.EmbeddingProvider != "gemini" {
		return fmt.Errorf("unknown EMBEDDING_PROVIDER")
	}
	if strings.TrimSpace(s.APIKey(s.EmbeddingProvider)) == "" {
		return fmt.Errorf("embedding API key required")
	}
	if s.EmbeddingProvider == "openai" && !strings.HasPrefix(strings.TrimSpace(s.OpenAIAPIKey), "sk-") {
		return fmt.Errorf("OPENAI_API_KEY must begin with sk-")
	}
	if strings.TrimSpace(s.RemnawaveMCPURL) == "" {
		return fmt.Errorf("REMNAWAVE_MCP_URL required")
	}
	if s.BedolagaMCPEnabled && strings.TrimSpace(s.BedolagaMCPURL) == "" {
		return fmt.Errorf("BEDOLAGA_MCP_URL required")
	}
	if s.BedolagaEnabled {
		if s.BedolagaAPIURL == "" || s.BedolagaAPIKey == "" || s.BedolagaWebhookSecret == "" {
			return fmt.Errorf("Bedolaga API URL, key and webhook secret required")
		}
		if !strings.HasPrefix(s.BedolagaWebhookPath, "/") || s.BedolagaPollIntervalSeconds < 1 || s.BedolagaMaxConcurrentTickets < 1 {
			return fmt.Errorf("invalid Bedolaga webhook path, poll interval or concurrency")
		}
	}
	return nil
}
