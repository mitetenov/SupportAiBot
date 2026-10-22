package bot

import (
	"context"
	"encoding/json"
	"io"
	"os"
	"reflect"
	"strings"
	"supportbot/internal/domain"
	"supportbot/internal/telegram"
	"sync"
	"testing"
	"time"
)

type traceFixture struct {
	Customers []struct {
		Name, Text, Response string
		Limited, Operator    bool
		Trace                []map[string]any
	}
	Commands map[string]string
}
type runtimeTrace struct {
	mu     sync.Mutex
	events []map[string]any
}

func (t *runtimeTrace) add(m map[string]any) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.events = append(t.events, m)
}

type tracedModel struct {
	domain.LLM
	trace    *runtimeTrace
	response string
}

func (m *tracedModel) Chat(_ context.Context, text string, user int64) (domain.Reply, error) {
	m.trace.add(map[string]any{"event": "model", "text": text, "user": user})
	return domain.Reply{Text: m.response}, nil
}

type tracedAPI struct {
	fakeAPI
	trace *runtimeTrace
}

func (a *tracedAPI) Call(ctx context.Context, method string, payload any, out any) error {
	if method == "sendMessage" {
		raw, _ := json.Marshal(payload)
		var p map[string]any
		json.Unmarshal(raw, &p)
		text, _ := p["text"].(string)
		text = telegram.StripHTML(text)
		user, _ := p["chat_id"].(float64)
		switch user {
		case 42:
			a.trace.add(map[string]any{"event": "send", "text": text, "user": int64(42)})
		case -100:
			_, answer, ok := strings.Cut(text, "\n\n")
			if ok {
				a.trace.add(map[string]any{"event": "forward", "text": answer, "user": int64(42), "escalate": strings.HasPrefix(text, "@operator ")})
			}
		}
	}
	return a.fakeAPI.Call(ctx, method, payload, out)
}
func (a *tracedAPI) Upload(context.Context, string, map[string]string, string, string, io.Reader, any) error {
	return nil
}

type tracedGaps struct {
	domain.Gaps
	trace *runtimeTrace
}

func (g *tracedGaps) Evaluate(_ context.Context, text string, id int64, _ string, _ domain.FAQContext) error {
	g.trace.add(map[string]any{"event": "gap", "text": text, "user": id})
	return nil
}
func TestPythonCustomerPipelineEventTrace(t *testing.T) {
	raw, e := os.ReadFile("testdata/python_runtime.json")
	if e != nil {
		t.Fatal(e)
	}
	var fixture traceFixture
	if e = json.Unmarshal(raw, &fixture); e != nil {
		t.Fatal(e)
	}
	for _, c := range fixture.Customers {
		t.Run(c.Name, func(t *testing.T) {
			trace := &runtimeTrace{}
			api := &tracedAPI{trace: trace}
			store := &memoryStore{topics: map[int64]domain.Topic{42: {UserID: 42, TopicID: 71}}}
			sender := telegram.NewSender(api)
			forward := &Forwarder{Sender: sender, Store: store, Group: -100, Admin: "operator"}
			state := NewConversationState(30*time.Minute, 6*time.Hour)
			if c.Operator {
				state.RecordOperator(42)
			}
			rate := NewRateLimiter()
			if c.Limited {
				rate.Acquire(42)
			}
			p := &Pipeline{LLM: &tracedModel{trace: trace, response: c.Response}, Sender: sender, Forwarder: forward, Rate: rate, Gaps: &tracedGaps{trace: trace}, State: state}
			p.Handle(context.Background(), Batch{Last: telegram.Message{ID: 1, Chat: telegram.Chat{ID: 42}}, User: &telegram.User{ID: 42}, IDs: []int64{1}, Text: c.Text, UserText: c.Text})
			actualJSON, _ := json.Marshal(trace.events)
			var actual []map[string]any
			json.Unmarshal(actualJSON, &actual)
			if !reflect.DeepEqual(actual, c.Trace) {
				t.Fatalf("Python trace%v\nGo trace%v", c.Trace, actual)
			}
		})
	}
	for key, want := range fixture.Commands {
		if got := Message(key); got != want {
			t.Errorf("message %s differs", key)
		}
	}
}
