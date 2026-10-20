package llm

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"supportbot/internal/domain"
	"testing"
)

func TestContextualSearchPreservesAnaphoricReferences(t *testing.T) {
	history := []domain.Message{
		{Role: "user", Content: "нужно ли оплатить подписку?"},
		{Role: "assistant", Content: "Подписка активна."},
		{Role: "user", Content: "дай ссылку на подписку"},
		{Role: "assistant", Content: "https://example.invalid/personal-placeholder"},
	}
	for _, text := range []string{
		"можно я дам ее поруге? она тоже хочет пользоваться вашим vpn",
		"можно передать её другу?", "МОЖНО ЕЕ ПЕРЕДАТЬ?",
		"можно ей пользоваться вместе?", "как подключить эту на телефон?",
		"можно пользоваться ею?", "что с ней делать?",
		"как пользоваться им?", "как пользоваться ими?",
		"что в нем?", "что в нём?",
	} {
		t.Run(text, func(t *testing.T) {
			want := "дай ссылку на подписку " + text
			if got := ContextualSearchQuery(text, history, false); got != want {
				t.Fatalf("lost referent: got %q, want %q", got, want)
			}
		})
	}
	for _, text := range []string{
		"хочу оплатить подписку", "сколько стоит тариф?",
		"подруге нужна отдельная подписка", "как купить подарок другу?",
		"реферальный бонус другу не пришел", "как включить семейный тариф?",
		"нужна реклама vpn", "кнопка подключения не работает",
	} {
		t.Run(text, func(t *testing.T) {
			if got := ContextualSearchQuery(text, history, false); got != text {
				t.Fatalf("new topic inherited unrelated history: %q", got)
			}
		})
	}
}

type incidentHistory struct {
	*fakeHistory
	messages []domain.Message
}

func (h *incidentHistory) GetHistory(context.Context, int64) ([]domain.Message, error) {
	return h.messages, nil
}

type incidentFAQ struct{ query, text string }

func (f *incidentFAQ) BuildFAQContext(_ context.Context, query string, _ map[string]bool) (domain.FAQContext, error) {
	f.query = query
	return domain.FAQContext{Text: f.text, Results: []domain.FAQResult{{Question: "Как поделиться подпиской с членом семьи?", Answer: f.text}}}, nil
}

func TestSharingTurnKeepsKnowledgeAndReasoningSeparate(t *testing.T) {
	const question = "можно я дам ее поруге? она тоже хочет пользоваться вашим vpn"
	const evidence = "Чтобы подключить устройство другого человека — передайте инструкцию из бота. Количество устройств зависит от тарифа; трафик и срок общие."
	const answer = "Да, можно подключить устройство другого человека по инструкции из бота. Лимит устройств зависит от тарифа; трафик и срок действия общие."
	const reasoning = "PRIVATE_REASONING_SENTINEL: вы не найдены, реклама, недопустимо продолжать"
	history := &incidentHistory{fakeHistory: &fakeHistory{}, messages: []domain.Message{
		{Role: "user", Content: "дай ссылку на подписку"},
		{Role: "assistant", Content: "https://example.invalid/personal-placeholder"},
	}}
	faq := &incidentFAQ{text: evidence}
	requests := 0
	client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		requests++
		var body Object
		raw, _ := io.ReadAll(req.Body)
		if err := json.Unmarshal(raw, &body); err != nil {
			t.Fatal(err)
		}
		messages := arr(body["messages"])
		if len(messages) != 5 || str(obj(messages[1])["role"]) != "system" || !strings.Contains(str(obj(messages[1])["content"]), evidence) {
			t.Fatal("retrieved knowledge was omitted or placed in conversational history")
		}
		if str(obj(messages[4])["content"]) != question || body["stream"] != false {
			t.Fatal("current question or nonstreaming contract changed")
		}
		if _, exists := body["tools"]; exists {
			t.Fatal("general policy question unexpectedly received personal tools")
		}
		payload := Object{"choices": []any{
			Object{"finish_reason": "stop", "message": Object{"content": answer, "reasoning_content": reasoning}},
			Object{"finish_reason": "stop", "message": Object{"content": "OTHER_CHOICE_SENTINEL"}},
		}}
		return httpJSON(200, jsonText(payload)), nil
	})}
	p, err := NewProvider(ProviderConfig{Name: "zai", Model: "glm-5.3-flash", APIKey: "fixture-only", BaseURL: "https://fixture.invalid", ReasoningEffort: "medium"}, nil, client)
	if err != nil {
		t.Fatal(err)
	}
	engine := &Engine{Providers: []*Provider{p}, History: history, FAQ: faq}
	reply, err := engine.Chat(context.Background(), question, 42)
	if err != nil {
		t.Fatal(err)
	}
	if faq.query != "дай ссылку на подписку "+question {
		t.Fatalf("retrieval lost subscription referent: %q", faq.query)
	}
	if requests != 1 || reply.Text != answer || history.lastAssistant != answer {
		t.Fatalf("terminal response mixed provider fields, retried or changed before persistence: requests=%d reply=%q saved=%q", requests, reply.Text, history.lastAssistant)
	}
}
