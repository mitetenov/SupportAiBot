package storage

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"supportbot/internal/domain"
	"testing"
)

func TestHistoryDefensiveCopyAndIsolation(t *testing.T) {
	h := NewHistory(nil, 2, 7)
	ctx := context.Background()
	for _, s := range []string{"a", "b", "c"} {
		if e := h.AddUserMessage(ctx, 1, s); e != nil {
			t.Fatal(e)
		}
	}
	got, _ := h.GetHistory(ctx, 1)
	got[0].Content = "mutate"
	got, _ = h.GetHistory(ctx, 1)
	if len(got) != 2 || got[0].Content != "b" {
		t.Fatal(got)
	}
	h.RecordFAQContext(1, domain.FAQContext{Results: []domain.FAQResult{{Question: "shown"}, {Question: "unused"}}}, []string{"shown"})
	h.RejectLastFAQ(1)
	if !h.GetRejectedFAQQuestions(1)["shown"] || h.GetRejectedFAQQuestions(1)["unused"] {
		t.Fatal("unattributed FAQ rejected")
	}
	h.ClearRejectedFAQsIfNewTopic(1, "Всё равно не работает")
	if !h.GetRejectedFAQQuestions(1)["shown"] {
		t.Fatal("rejection lost state")
	}
	h.ClearRejectedFAQsIfNewTopic(1, "Как подключить другое устройство?")
	if len(h.GetRejectedFAQQuestions(1)) != 0 {
		t.Fatal("new topic retained rejected context")
	}
	h.AddUserMessage(ctx, 2, "other")
	h.Clear(ctx, 1)
	v, _ := h.GetHistory(ctx, 2)
	if len(v) != 1 {
		t.Fatal("cross user clear")
	}
}
func TestRejectionBoundaries(t *testing.T) {
	for _, x := range []string{"Не только", "другое устройство", "не товарищ", "подключить другое"} {
		if IsRejection(x) {
			t.Fatal("false rejection", x)
		}
	}
	for _, x := range []string{"другое!", "это не то.", "Всё ещё не заработало", "по-прежнему"} {
		if !IsRejection(x) {
			t.Fatal("missed rejection", x)
		}
	}
	_ = strings.TrimSpace
}
func TestPythonDifferentialRejection(t *testing.T) {
	raw, e := os.ReadFile("testdata/python_rejection.json")
	if e != nil {
		t.Fatal(e)
	}
	var rows []struct {
		Text     string
		Rejected bool
	}
	if e = json.Unmarshal(raw, &rows); e != nil {
		t.Fatal(e)
	}
	for _, r := range rows {
		if got := IsRejection(r.Text); got != r.Rejected {
			t.Errorf("%q: %v != %v", r.Text, got, r.Rejected)
		}
	}
}
