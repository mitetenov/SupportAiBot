package rag_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"supportbot/internal/domain"
	"supportbot/internal/llm"
	"supportbot/internal/rag"
	"testing"
)

type article struct {
	Question string   `json:"question"`
	Answer   string   `json:"answer"`
	Keywords []string `json:"keywords"`
	Image    string   `json:"image"`
}

func articles(t *testing.T) []article {
	t.Helper()
	raw, e := os.ReadFile("../../faq/faq.json")
	if e != nil {
		t.Fatal(e)
	}
	var entries []article
	if e = json.Unmarshal(raw, &entries); e != nil {
		t.Fatal(e)
	}
	if len(entries) < 54 {
		t.Fatal("FAQ entries missing")
	}
	return entries
}
func findArticle(t *testing.T, entries []article, needles ...string) article {
	t.Helper()
	for _, e := range entries {
		text := strings.ToLower(e.Question + " " + strings.Join(e.Keywords, " "))
		ok := true
		for _, needle := range needles {
			ok = ok && strings.Contains(text, strings.ToLower(needle))
		}
		if ok {
			return e
		}
	}
	t.Fatalf("missing FAQ %v", needles)
	return article{}
}
func containsAll(t *testing.T, text string, parts ...string) {
	t.Helper()
	for _, part := range parts {
		if !strings.Contains(strings.ToLower(text), strings.ToLower(part)) {
			t.Errorf("missing confirmed fact %q", part)
		}
	}
}
func ordered(t *testing.T, text string, parts ...string) {
	t.Helper()
	last := -1
	for _, part := range parts {
		i := strings.Index(text, part)
		if i < 0 || i <= last {
			t.Errorf("incorrect instruction order: %q", part)
		}
		last = i
	}
}
func TestFAQStructureAssetsAndRemovedInstructions(t *testing.T) {
	seen := map[string]bool{}
	for _, e := range articles(t) {
		if strings.TrimSpace(e.Question) == "" || strings.TrimSpace(e.Answer) == "" || e.Keywords == nil || seen[e.Question] {
			t.Error("malformed or duplicate FAQ", e.Question)
		}
		seen[e.Question] = true
		if e.Image != "" {
			if _, err := os.Stat(filepath.Join("../../faq/images", e.Image)); err != nil {
				t.Error("missing FAQ image", e.Image)
			}
		}
		for _, wrong := range []string{"три точки", "слева от надписи vpn", "«баланс и платежи»", "«подписки»"} {
			if strings.Contains(strings.ToLower(e.Answer), wrong) {
				t.Error("obsolete FAQ instruction", e.Question, wrong)
			}
		}
		if strings.Contains(e.Answer, "«Обновить подписку»") && strings.Contains(e.Answer, "«Пинг»") && e.Image == "" {
			t.Error("button instruction has no image", e.Question)
		}
	}
	for _, wrong := range []string{"Только главный экран", "три точки"} {
		if strings.Contains(llm.SystemPrompt, wrong) {
			t.Error("obsolete prompt instruction", wrong)
		}
	}
}
func TestConfirmedFAQPolicies(t *testing.T) {
	entries := articles(t)
	tests := []struct{ needles, required []string }{
		{[]string{"автопродление"}, []string{"автоматического списания с банковской карты", "нет", "баланс", "«Автоплатеж» → «Включить»", "переключатель «Автопродление»", "только для действующей оплаченной подписки", "не покупает первый платный период после триала", "не возобновляет уже истёкшую подписку"}},
		{[]string{"ссылк", "копировать"}, []string{"отправит", "вручную", "@PeipivoSalesBot", "lk.peipivo.top"}},
		{[]string{"нет серверов"}, []string{"первая кнопка", "только устанавливает приложение", "вторую кнопку", "Добавить подписку", "@PeipivoSalesBot", "https://lk.peipivo.top"}},
		{[]string{"tribute"}, []string{"больше не", "отпишитесь"}},
		{[]string{"подар", "нет аккаунта"}, []string{"аккаунт", "пробную подписку необязательно"}},
		{[]string{"telegram id"}, []string{"@myidbot", "номер"}},
		{[]string{"не грузится"}, []string{"proxy"}},
		{[]string{"где кнопка обновить"}, []string{"в её правой части"}},
		{[]string{"ошибка при пополнении"}, []string{"этапе", "другой способ пополнения", "@peipivosalesbot"}},
		{[]string{"прокси", "не работает"}, []string{"отдельно от VPN-подписки"}},
	}
	for _, tc := range tests {
		t.Run(strings.Join(tc.needles, "/"), func(t *testing.T) {
			e := findArticle(t, entries, tc.needles...)
			containsAll(t, e.Answer, tc.required...)
		})
	}
	containsAll(t, strings.Join(findArticle(t, entries, "автопродление").Keywords, " "), "отвязать карту")
	if findArticle(t, entries, "где кнопка обновить").Image == "" {
		t.Error("button guide has no image")
	}
	payment := strings.ToLower(findArticle(t, entries, "ошибка при пополнении").Answer)
	if !strings.Contains(payment, "выбор") && !strings.Contains(payment, "проведен") {
		t.Error("missing payment conditions")
	}
	containsAll(t, entries[5].Answer, "«Баланс»", "«Подписка»")
	for _, i := range []int{8, 9, 12} {
		containsAll(t, entries[i].Answer, "20 ГБ", "оплачен")
	}
	for _, i := range []int{19, 35} {
		containsAll(t, entries[i].Answer, "windows", "подписк", "приложени", "сервер", "активен", "последн")
	}
	containsAll(t, entries[10].Answer, "Linux", "на странице своей подписки")
	containsAll(t, entries[53].Answer, "В Happ и Incy кнопки расположены одинаково")
	if strings.Contains(findArticle(t, entries, "прокси", "не работает").Answer, "Обновите подписку") {
		t.Error("proxy instructions require VPN")
	}
}
func TestConfirmedDiagnosisOrderAndPlatforms(t *testing.T) {
	entries := articles(t)
	for _, i := range []int{0, 27, 35, 36, 38, 39, 40} {
		answer := entries[i].Answer
		ordered(t, answer, "«Обновить подписку»", "«Пинг»", "обновите Happ", "https://google.com", "https://ya.ru")
		containsAll(t, answer, "строго с выключенным VPN", "ошибка обновления подписки или все серверы", "в Incy пропустите обновление приложения", "Если Happ уже последней версии, этот шаг тоже пропустите")
	}
	for _, i := range []int{0, 35, 36, 38} {
		answer := entries[i].Answer
		_, tail, ok := strings.Cut(answer, "Если Google открылся")
		if !ok {
			_, tail, _ = strings.Cut(answer, "Если после этих шагов")
		}
		if strings.Contains(tail, "нажмите") {
			t.Error("repeated completed refresh/ping", i)
		}
		containsAll(t, answer, "ошибки и не все серверы")
	}
	generic := findArticle(t, entries, "Серверы показывают n/a").Answer
	containsAll(t, generic, "iOS", "iPadOS", "macOS", "Windows", "Android", "в Incy пропустите обновление приложения")
	ordered(t, generic, "Если приложение неизвестно", "напишите вашу ОС")
	if strings.Contains(generic, "обновите приложение Happ до последней версии или установите") {
		t.Error("premature app switch")
	}
	apple := findArticle(t, entries, "Все серверы n/a на iPhone").Answer
	containsAll(t, apple, "iOS", "iPadOS", "macOS", "установите Incy", "@PeipivoSalesBot", "https://lk.peipivo.top", "в обновлённом Happ", "Если уже используете Incy", "повторную установку или обновление приложения на этом этапе не предлагайте")
	ordered(t, apple, "обновите Happ", "https://google.com", "установите Incy")
	windows := findArticle(t, entries, "Все серверы n/a на Windows").Answer
	containsAll(t, windows, "Windows", "Android", "обновите Happ до последней версии", "Happ последней версии", "можно переустановить Happ", "Переустановку не предлагайте до проверки Google", "@PeipivoSalesBot", "https://lk.peipivo.top", "Этот шаг относится к Happ", "в Incy пропустите обновление приложения")
	ordered(t, windows, "https://google.com", "можно переустановить Happ")
	prompt := llm.SystemPrompt
	containsAll(t, prompt, "верни саму ссылку целиком без изменений", "TUN", "Proxy", "выясни используемое приложение, если оно неизвестно", "Для Happ — обновить Happ", "Для Incy — пропустить обновление приложения", "При оставшихся n/a уточни ОС", "не советуй переустановку/смену приложения")
	_, diagnosis, ok := strings.Cut(prompt, "ДИАГНОСТИКА:")
	if !ok {
		t.Fatal("diagnosis policy missing")
	}
	diagnosis, _, _ = strings.Cut(diagnosis, "УСТРОЙСТВА И ЭСКАЛАЦИЯ:")
	ordered(t, diagnosis, "«Обновить подписку»", "«Пинг»", "обновить Happ", "https://google.com")
	containsAll(t, diagnosis, "ошибка обновления подписки или все серверы n/a", "Давай только следующий невыполненный шаг", "дождись его результата", "известное не переспрашивай")
	selected := []domain.FAQResult{}
	for _, i := range []int{0, 35, 38} {
		e := entries[i]
		selected = append(selected, domain.FAQResult{Question: e.Question, Answer: e.Answer, Image: e.Image})
	}
	text := rag.FormatFAQContext(selected)
	if strings.Count(text, "Напишите, появилась ли ошибка обновления и что показал пинг") != 1 {
		t.Error("common instructions duplicated")
	}
	ordered(t, text, "«Обновить подписку»", "«Пинг»", "обновите Happ", "https://google.com")
	containsAll(t, text, "в Incy пропустите обновление приложения")
}
