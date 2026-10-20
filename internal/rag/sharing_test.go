package rag

import (
	"context"
	"encoding/json"
	"os"
	"slices"
	"strings"
	"testing"
)

func TestSharingIntentRequiresExistingArticle(t *testing.T) {
	for _, query := range []string{
		"дай ссылку на подписку можно я дам ее поруге? она тоже хочет пользоваться вашим vpn",
		"можно поделиться подпиской с подругой?",
		"передам ссылку другу",
		"можем вместе пользоваться одной подпиской?",
		"как подключить маму к моей подписке?",
		"хочу отправить ключ другому человеку",
		"то есть подруге нельзя пользоваться моей подпиской?",
		"может ли мама пользоваться моей подпиской?",
	} {
		t.Run(query, func(t *testing.T) {
			if !slices.Contains(RequiredIDs(query), "subscription_sharing") {
				t.Fatal("verified sharing article omitted")
			}
		})
	}
	for _, query := range []string{
		"дай ссылку на подписку",
		"как подключить приложение к моей подписке?",
		"как пригласить друга и получить реферальный бонус?",
		"как поделиться реферальной ссылкой на VPN с другом?",
		"как войти в чужой аккаунт по ссылке?",
		"можно дать доступ к чужому аккаунту?",
		"можно передать чужую ссылку на VPN другу без согласия?",
		"передам ссылку подруги без её согласия",
		"как поделиться реферальной ссылкой на покупку подписки с другом?",
		"подруга не может подключиться к VPN, ошибка",
		"как оплатить другу подписку?",
		"VPN не работает на моём телефоне",
		"что подарить подруге?",
	} {
		t.Run(query, func(t *testing.T) {
			if slices.Contains(RequiredIDs(query), "subscription_sharing") {
				t.Fatal("unrelated referral/account intent treated as subscription sharing")
			}
		})
	}
}

func TestLegacySharingArticleRetainsIndexAndOverridesRetrievalMiss(t *testing.T) {
	store := ragDB(t)
	ctx := context.Background()
	embedding := &fakeEmbedding{model: "sharing-regression"}
	// Use the unchanged repository article, including its legacy absence of an ID.
	raw, err := os.ReadFile("../../faq/faq.json")
	if err != nil {
		t.Fatal(err)
	}
	var catalog []catalogEntry
	if err = json.Unmarshal(raw, &catalog); err != nil {
		t.Fatal(err)
	}
	var article catalogEntry
	for _, row := range catalog {
		if row.Question == sharingFAQQuestion {
			article = row
		}
	}
	if article.Question == "" || article.ID != "" {
		t.Fatal("expected original legacy sharing FAQ")
	}
	encoded, _ := json.Marshal([]catalogEntry{article})
	path := t.TempDir() + "/faq.json"
	if err = os.WriteFile(path, encoded, 0600); err != nil {
		t.Fatal(err)
	}
	first := New(store, embedding)
	if err = first.Initialize(ctx, path); err != nil {
		t.Fatal(err)
	}
	calls := embedding.calls.Load()
	second := New(store, embedding)
	if err = second.Initialize(ctx, path); err != nil {
		t.Fatal(err)
	}
	if embedding.calls.Load() != calls {
		t.Fatal("in-memory ID mapping forced paid reindex on restart")
	}
	// Simulate the observed semantic retrieval miss independently of embeddings.
	second.mu.Lock()
	second.ready = false
	second.mu.Unlock()
	query := "дай ссылку на подписку можно я дам ее поруге? она тоже хочет пользоваться вашим vpn"
	got, err := second.BuildFAQContext(ctx, query, nil)
	if err != nil || len(got.Results) != 1 || got.Results[0].Answer != article.Answer || !strings.Contains(got.Text, article.Answer) {
		t.Fatalf("grounded context lost the existing sharing instructions: %#v %v", got, err)
	}
	got, err = second.BuildFAQContext(ctx, query, map[string]bool{sharingFAQQuestion: true})
	if err != nil || len(got.Results) != 0 {
		t.Fatal("explicitly excluded article should remain excluded")
	}
}
