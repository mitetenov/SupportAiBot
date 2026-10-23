package rag

import (
	"slices"
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

// Use the unchanged repository article, including its legacy absence of an ID.

// Simulate the observed semantic retrieval miss independently of embeddings.
