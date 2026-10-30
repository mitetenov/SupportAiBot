package llm

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"supportbot/internal/mcp"
	"supportbot/internal/storage"
	"testing"
)

const deviceListEvidence = `{"response":{"accounts":[{"userId":17000001,"devices":[{"hwid":"android-private","deviceModel":"Android"},{"hwid":"tv-private","deviceModel":"Android TV"}]},{"userId":17000002,"devices":[{"hwid":"windows-private","deviceModel":"Windows"}]}]}}`

type deviceClient struct {
	calls     []string
	deleted   []string
	deleteErr error
}

func (c *deviceClient) ServerName() string { return "remnawave" }
func (c *deviceClient) ListTools() []mcp.Tool {
	return []mcp.Tool{
		{Name: "hwid_devices_list", InputSchema: Object{"type": "object", "properties": Object{"telegramId": Object{"type": "number"}}}},
		{Name: "hwid_device_delete", InputSchema: Object{"type": "object", "properties": Object{"telegramId": Object{"type": "number"}, "userId": Object{"type": "number"}, "hwid": Object{"type": "string"}}}},
	}
}
func (c *deviceClient) CallTool(_ context.Context, name string, args map[string]any) (string, error) {
	c.calls = append(c.calls, name)
	if args["telegramId"] != int64(9000000001) {
		return "", fmt.Errorf("unpinned caller: %v", args)
	}
	switch name {
	case "hwid_devices_list":
		return deviceListEvidence, nil
	case "users_get_by_telegram_id":
		return `{"response":{"users":[{"id":17000001,"telegramId":9000000001},{"id":17000002,"telegramId":9000000001}]}}`, nil
	case "hwid_device_delete":
		c.deleted = append(c.deleted, fmt.Sprintf("%.0f/%s", args["userId"], args["hwid"]))
		return `{"status":"deleted"}`, c.deleteErr
	default:
		return "", fmt.Errorf("unexpected tool %s", name)
	}
}

func deviceProviderResponse(provider, text string, calls ...ToolCall) string {
	parts := []any{}
	for _, call := range calls {
		switch provider {
		case "openai":
			parts = append(parts, Object{"type": "function_call", "call_id": call.ID, "name": call.Name, "arguments": jsonText(call.Arguments)})
		case "gemini":
			parts = append(parts, Object{"functionCall": Object{"id": call.ID, "name": call.Name, "args": call.Arguments}})
		default:
			parts = append(parts, Object{"id": call.ID, "type": "function", "function": Object{"name": call.Name, "arguments": jsonText(call.Arguments)}})
		}
	}
	switch provider {
	case "openai":
		if text != "" {
			parts = append(parts, Object{"type": "message", "content": []any{Object{"type": "output_text", "text": text}}})
		}
		return jsonText(Object{"output": parts})
	case "gemini":
		if text != "" {
			parts = append(parts, Object{"text": text})
		}
		return jsonText(Object{"candidates": []any{Object{"content": Object{"parts": parts}}}})
	default:
		finish := "stop"
		message := Object{"content": text}
		if len(calls) > 0 {
			finish = "tool_calls"
			message["tool_calls"] = parts
		}
		return jsonText(Object{"choices": []any{Object{"finish_reason": finish, "message": message}}})
	}
}

func TestDeviceSelectionAcrossMessages(t *testing.T) {
	for _, provider := range []string{"openai", "gemini", "zai"} {
		t.Run(provider, func(t *testing.T) {
			ctx := context.Background()
			history := storage.NewHistory(nil, 20, 7)
			client := &deviceClient{}
			router := mcp.NewRouter([]mcp.Client{client}, false)
			requests := 0
			httpClient := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				requests++
				raw, _ := io.ReadAll(req.Body)
				var response string
				switch requests {
				case 1:
					response = deviceProviderResponse(provider, "", ToolCall{ID: "list", Name: "hwid_devices_list", Arguments: Object{}})
				case 2:
					response = deviceProviderResponse(provider, "1. Android\n2. Android TV\n3. Windows\nКакие устройства удалить?")
				case 3:
					for _, evidence := range []string{"Сохранённый результат hwid_devices_list", "17000001", "17000002", "tv-private", "windows-private"} {
						if !strings.Contains(string(raw), evidence) {
							t.Errorf("follow-up lost device evidence %q", evidence)
						}
					}
					response = deviceProviderResponse(provider, "",
						ToolCall{ID: "tv", Name: "hwid_device_delete", Arguments: Object{"userId": 17000001, "hwid": "tv-private"}},
						ToolCall{ID: "windows", Name: "hwid_device_delete", Arguments: Object{"userId": 17000002, "hwid": "windows-private"}})
				case 4:
					response = deviceProviderResponse(provider, "Устройства 2 и 3 удалены.")
				default:
					t.Fatalf("unexpected provider request %d", requests)
				}
				return httpJSON(200, response), nil
			})}
			p, err := NewProvider(ProviderConfig{Name: provider, Model: map[string]string{"openai": "gpt-6-luna", "gemini": "gemini-3-flash-preview", "zai": "glm-5.3-flash"}[provider], APIKey: "test", BaseURL: "https://provider"}, router.ListTools, httpClient)
			if err != nil {
				t.Fatal(err)
			}
			engine := &Engine{Providers: []*Provider{p}, Router: router, History: history}
			if _, err := engine.Chat(ctx, "Превышен лимит устройств", 9000000001); err != nil {
				t.Fatal(err)
			}
			if snapshot, err := history.GetDeviceSnapshot(ctx, 9000000002); err != nil || snapshot != "" {
				t.Fatalf("device snapshot leaked to another user: %s %v", snapshot, err)
			}
			// A new engine represents a separate message, not the same tool loop.
			engine = &Engine{Providers: []*Provider{p}, Router: router, History: history}
			reply, err := engine.Chat(ctx, "2 и 3 удалить", 9000000001)
			if err != nil || reply.Text != "Устройства 2 и 3 удалены." {
				t.Fatalf("follow-up failed: %+v %v", reply, err)
			}
			if !reflect.DeepEqual(client.deleted, []string{"17000001/tv-private", "17000002/windows-private"}) {
				t.Fatalf("wrong device pairs deleted: %v", client.deleted)
			}
			want := []string{"hwid_devices_list", "users_get_by_telegram_id", "hwid_device_delete", "users_get_by_telegram_id", "hwid_device_delete"}
			if !reflect.DeepEqual(client.calls, want) {
				t.Fatalf("ownership checks skipped or tool calls repeated: %v", client.calls)
			}
			if snapshot, err := history.GetDeviceSnapshot(ctx, 9000000001); err != nil || snapshot != "" {
				t.Fatalf("deletion retained stale device evidence: %s %v", snapshot, err)
			}
			messages, _ := history.GetHistory(ctx, 9000000001)
			if len(messages) != 4 || strings.Contains(jsonText(messages), "private") {
				t.Fatalf("technical evidence leaked into customer text history: %v", messages)
			}
		})
	}
}

func TestDeviceSnapshotRefreshRejectsFailedOrMalformedLists(t *testing.T) {
	for _, result := range []string{`{"error":"unavailable"}`, `{"ok":false,"response":{"accounts":[]}}`, `{"response":{"accounts":null}}`, `{"response":`, `null`} {
		ctx := context.Background()
		history := storage.NewHistory(nil, 20, 7)
		engine := &Engine{History: history}
		if err := history.SetDeviceSnapshot(ctx, 42, "old evidence"); err != nil {
			t.Fatal(err)
		}
		if err := engine.invalidateDeviceSnapshot(ctx, 42, "hwid_devices_list"); err != nil {
			t.Fatal(err)
		}
		if snapshot := buildDeviceSnapshot(ToolCall{Name: "hwid_devices_list"}, result); snapshot != "" {
			t.Fatalf("failed refresh created device evidence: %s", snapshot)
		}
		if snapshot, _ := history.GetDeviceSnapshot(ctx, 42); snapshot != "" {
			t.Fatalf("failed refresh reused device evidence: %s", snapshot)
		}
	}
}

func TestDeviceSnapshotPreservesFullModeAccountAndExactIDs(t *testing.T) {
	result := `{"response":{"devices":[{"hwid":"opaque-device","largeId":9007199254740993}]}}`
	call := ToolCall{Name: "hwid_devices_list", Arguments: Object{"userId": 17, "telegramId": "forged-caller"}}
	snapshot := buildDeviceSnapshot(call, result)
	if !strings.Contains(snapshot, `"userId":17`) || !strings.Contains(snapshot, "9007199254740993") || strings.Contains(snapshot, "forged-caller") {
		t.Fatalf("snapshot lost account/precision or retained forged caller: %s", snapshot)
	}
}

func TestDeviceEvidenceSurvivesFallbackAfterPartialDeletion(t *testing.T) {
	ctx := context.Background()
	history := storage.NewHistory(nil, 20, 7)
	snapshot := buildDeviceSnapshot(ToolCall{Name: "hwid_devices_list"}, deviceListEvidence)
	if err := history.SetDeviceSnapshot(ctx, 9000000001, snapshot); err != nil {
		t.Fatal(err)
	}
	client := &deviceClient{}
	router := mcp.NewRouter([]mcp.Client{client}, false)
	primaryCalls, fallbackCalls := 0, 0
	tv := ToolCall{ID: "tv", Name: "hwid_device_delete", Arguments: Object{"userId": 17000001, "hwid": "tv-private"}}
	windows := ToolCall{ID: "windows", Name: "hwid_device_delete", Arguments: Object{"userId": 17000002, "hwid": "windows-private"}}
	httpClient := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		if req.URL.Host == "primary" {
			primaryCalls++
			if primaryCalls == 1 {
				return httpJSON(200, deviceProviderResponse("openai", "", tv)), nil
			}
			return httpJSON(413, `{"error":{"message":"context too large"}}`), nil
		}
		fallbackCalls++
		raw, _ := io.ReadAll(req.Body)
		if !strings.Contains(string(raw), "windows-private") || !strings.Contains(string(raw), "Сохранённый результат hwid_devices_list") {
			t.Error("fallback lost device evidence after first deletion")
		}
		if fallbackCalls == 1 {
			return httpJSON(200, deviceProviderResponse("zai", "", tv, windows)), nil
		}
		return httpJSON(200, deviceProviderResponse("zai", "Устройства удалены.")), nil
	})}
	primary, _ := NewProvider(ProviderConfig{Name: "openai", Model: "gpt-6-luna", BaseURL: "https://primary", APIKey: "test"}, router.ListTools, httpClient)
	fallback, _ := NewProvider(ProviderConfig{Name: "zai", Model: "glm-5.3-flash", BaseURL: "https://fallback", APIKey: "test"}, router.ListTools, httpClient)
	engine := &Engine{Providers: []*Provider{primary, fallback}, Router: router, History: history}
	if _, err := engine.Chat(ctx, "2 и 3 удалить", 9000000001); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(client.deleted, []string{"17000001/tv-private", "17000002/windows-private"}) || primaryCalls != 2 || fallbackCalls != 2 {
		t.Fatalf("deletion was replayed or incomplete: %v, provider calls %d/%d", client.deleted, primaryCalls, fallbackCalls)
	}
}

func TestUnknownDeviceDeletionInvalidatesSnapshotAndCannotFallback(t *testing.T) {
	ctx := context.Background()
	history := storage.NewHistory(nil, 20, 7)
	if err := history.SetDeviceSnapshot(ctx, 9000000001, buildDeviceSnapshot(ToolCall{Name: "hwid_devices_list"}, deviceListEvidence)); err != nil {
		t.Fatal(err)
	}
	client := &deviceClient{deleteErr: errors.New("write response lost")}
	router := mcp.NewRouter([]mcp.Client{client}, false)
	requests := 0
	httpClient := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		requests++
		return httpJSON(200, deviceProviderResponse("openai", "", ToolCall{ID: "tv", Name: "hwid_device_delete", Arguments: Object{"userId": 17000001, "hwid": "tv-private"}})), nil
	})}
	p, _ := NewProvider(ProviderConfig{Name: "openai", Model: "gpt-6-luna", BaseURL: "https://provider", APIKey: "test"}, router.ListTools, httpClient)
	engine := &Engine{Providers: []*Provider{p, p}, Router: router, History: history}
	if _, err := engine.Chat(ctx, "2 удалить", 9000000001); err == nil || FallbackEligible(err) {
		t.Fatalf("unknown deletion outcome allowed fallback: %v", err)
	}
	if requests != 1 || len(client.deleted) != 1 {
		t.Fatal("unknown deletion was replayed")
	}
	if snapshot, err := history.GetDeviceSnapshot(ctx, 9000000001); err != nil || snapshot != "" {
		t.Fatalf("unknown deletion retained stale device evidence: %s %v", snapshot, err)
	}
}

func TestFailedAnswerDoesNotPublishNewDeviceSnapshot(t *testing.T) {
	ctx := context.Background()
	history := storage.NewHistory(nil, 20, 7)
	if err := history.SetDeviceSnapshot(ctx, 9000000001, "previous list"); err != nil {
		t.Fatal(err)
	}
	router := mcp.NewRouter([]mcp.Client{&deviceClient{}}, false)
	requests := 0
	httpClient := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		requests++
		if requests == 1 {
			return httpJSON(200, deviceProviderResponse("openai", "", ToolCall{ID: "list", Name: "hwid_devices_list", Arguments: Object{}})), nil
		}
		return httpJSON(200, `{"output":[]}`), nil
	})}
	p, _ := NewProvider(ProviderConfig{Name: "openai", Model: "gpt-6-luna", BaseURL: "https://provider", APIKey: "test"}, router.ListTools, httpClient)
	engine := &Engine{Providers: []*Provider{p}, Router: router, History: history}
	if _, err := engine.Chat(ctx, "Покажи устройства", 9000000001); err == nil {
		t.Fatal("malformed provider answer unexpectedly succeeded")
	}
	if snapshot, err := history.GetDeviceSnapshot(ctx, 9000000001); err != nil || snapshot != "" {
		t.Fatalf("failed answer published a list the user never saw: %s %v", snapshot, err)
	}
}
