package telegram

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
	"unicode/utf8"
)

func TestPythonFormattingDifferential(t *testing.T) {
	b, e := os.ReadFile("testdata/python_formatting.json")
	if e != nil {
		t.Fatal(e)
	}
	var cases []struct{ Input, Expected string }
	if e = json.Unmarshal(b, &cases); e != nil {
		t.Fatal(e)
	}
	for _, c := range cases {
		if got := MarkdownHTML(c.Input); got != c.Expected {
			t.Errorf("input=%q\nexpected=%q\ngot=%q", c.Input, c.Expected, got)
		}
	}
}
func TestHTMLChunksBalanceAndPreserveLongAnswer(t *testing.T) {
	text := "**" + strings.Repeat("я", 10000) + "**"
	chunks := SplitHTML(MarkdownHTML(text), 4096)
	if len(chunks) < 3 {
		t.Fatal("not split")
	}
	var content string
	for _, chunk := range chunks {
		if utf8.RuneCountInString(chunk) > 4096 {
			t.Fatal("oversized chunk")
		}
		if !strings.HasPrefix(chunk, "<b>") || !strings.HasSuffix(chunk, "</b>") {
			t.Fatalf("unbalanced %.30s", chunk)
		}
		content += StripHTML(chunk)
	}
	if content != strings.Repeat("я", 10000) {
		t.Fatal("answer text lost")
	}
}
func TestPythonChunkDifferential(t *testing.T) {
	b, e := os.ReadFile("testdata/python_chunks.json")
	if e != nil {
		t.Fatal(e)
	}
	var cases []struct {
		Input    string
		Limit    int
		Expected []string
	}
	if e = json.Unmarshal(b, &cases); e != nil {
		t.Fatal(e)
	}
	for _, c := range cases {
		got := SplitHTML(c.Input, c.Limit)
		a, _ := json.Marshal(got)
		b, _ := json.Marshal(c.Expected)
		if string(a) != string(b) {
			t.Errorf("chunks differ limit%d expected%s got%s", c.Limit, b, a)
		}
	}
}
