package config

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func TestDefaultsAndAliases(t *testing.T) {
	env := map[string]string{"LOG_LEVEL": " trace ", "TELEGRAM_SUPPORT_GROUP_CHAT_ID": "-1005000000001", "TELEGRAM_SUPPORT_ADMIN_TELEGRAM_IDS": "1, invalid, 5000000001", "LLM_FALLBACK_CHAIN": " groq:model:a , zai:model-b ", "OPENAI_TEMPERATURE": "0.25"}
	s, e := FromEnv(func(k string) (string, bool) { v, ok := env[k]; return v, ok })
	if e != nil {
		t.Fatal(e)
	}
	if s.BotLogLevel != "TRACE" || s.TelegramSupportGroupChatID != -1005000000001 || !s.TelegramSupportAdminTelegramIDs[5000000001] || s.OpenAITemperature == nil || *s.OpenAITemperature != 0.25 || s.LLMFallbackChain[0].Model != "model:a" {
		t.Fatalf("config drift %#v", s)
	}
	if s.ChatHistoryMaxMessages != 20 || s.TelegramBufferWindowMS != 2500 || s.BedolagaMaxConcurrentTickets != 5 {
		t.Fatal("defaults drift")
	}
}
func TestInvalidSettingsDoNotExposeSecrets(t *testing.T) {
	s := Default()
	s.TelegramBotToken = "123456789:secret-sentinel"
	s.TelegramSupportGroupChatID = -100
	s.OpenAIAPIKey = "secret-not-sk"
	s.GeminiAPIKey = "gemini-sentinel"
	if e := s.Validate(); e == nil || strings.Contains(e.Error(), "secret-not-sk") {
		t.Fatal("credential validation leaks secret")
	}
	s.OpenAIAPIKey = "sk-valid"
	s.OpenRouterAPIKey = "test"
	s.LLMFallbackChain = []ProviderTarget{{"openrouter", "test"}}
	for _, base := range []string{"https://user:pass@api.example/v1", "https://api.example/v1?key=x", "https://api.example/v1/chat/completions", "file:///tmp/x"} {
		s.OpenRouterBaseURL = base
		if e := s.Validate(); e == nil {
			t.Fatal("accepted unsafe URL", base)
		}
	}
}
func TestPythonDifferentialValidation(t *testing.T) {
	raw, e := os.ReadFile("testdata/python_config.json")
	if e != nil {
		t.Fatal(e)
	}
	var rows []struct {
		Env   map[string]string
		Valid bool
	}
	if e = json.Unmarshal(raw, &rows); e != nil {
		t.Fatal(e)
	}
	for _, row := range rows {
		s, e := FromEnv(func(k string) (string, bool) { v, ok := row.Env[k]; return v, ok })
		if e == nil {
			e = s.Validate()
		}
		if (e == nil) != row.Valid {
			t.Errorf("validation drift env=%v want=%v error=%v", row.Env, row.Valid, e)
		}
	}
}
