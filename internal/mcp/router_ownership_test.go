package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"reflect"
	"strconv"
	"testing"
)

func TestOwnedAccountToolsWithLargeTelegramIDs(t *testing.T) {
	data, err := os.ReadFile("testdata/remnawave_tools.json")
	if err != nil {
		t.Fatal(err)
	}
	var tools []Tool
	if err := json.Unmarshal(data, &tools); err != nil {
		t.Fatal(err)
	}
	for _, key := range []int64{42, 999999, 1000000, 123456789, 9000000001, 4503599627370495} {
		for _, quoted := range []bool{false, true} {
			t.Run(fmt.Sprintf("%d/string=%t", key, quoted), func(t *testing.T) {
				telegramID := strconv.FormatInt(key, 10)
				if quoted {
					telegramID = strconv.Quote(telegramID)
				}
				rem := &mockClient{name: "remnawave", tools: tools, results: map[string]string{
					"users_get_by_telegram_id": fmt.Sprintf(`{"response":{"users":[{"id":17000001,"telegramId":%s}]}}`, telegramID),
					"hwid_devices_list":        `{"response":{"accounts":[{"userId":17000001,"devices":[{"hwid":"current-device"}]}]}}`,
					"users_get":                `{"response":{"id":17000001}}`,
					"hwid_device_delete":       `{"status":"deleted"}`,
				}}
				r := NewRouter([]Client{rem}, false)
				ctx := context.Background()
				if result, err := r.CallTool(ctx, "hwid_devices_list", nil, key); err != nil || !contains(result, "current-device") {
					t.Fatalf("device list failed: %s %v", result, err)
				}
				for _, tool := range []string{"users_get", "hwid_device_delete"} {
					args := map[string]any{"userId": float64(17000001), "telegramId": float64(7), "hwid": "current-device"}
					result, err := r.CallTool(ctx, tool, args, key)
					if err != nil || result != rem.results[tool] {
						t.Fatalf("owned %s rejected: %s %v", tool, result, err)
					}
					forwarded := rem.args[len(rem.args)-1]
					if forwarded["userId"] != args["userId"] || forwarded["telegramId"] != key || forwarded["hwid"] != "current-device" {
						t.Fatalf("wrong forwarded identity/device: %v", forwarded)
					}
					if args["telegramId"] != float64(7) {
						t.Fatal("model arguments mutated")
					}
				}
				wantCalls := []string{"hwid_devices_list", "users_get_by_telegram_id", "users_get", "users_get_by_telegram_id", "hwid_device_delete"}
				if !reflect.DeepEqual(rem.calls, wantCalls) {
					t.Fatalf("calls = %v, want %v", rem.calls, wantCalls)
				}
				for _, index := range []int{1, 3} {
					if !reflect.DeepEqual(rem.args[index], map[string]any{"telegramId": key}) {
						t.Fatalf("ownership lookup not pinned: %v", rem.args[index])
					}
				}
			})
		}
	}
}

func TestForeignOrUnverifiedAccountToolsStayBlocked(t *testing.T) {
	for _, tc := range []struct {
		name   string
		key    int64
		wanted any
		lookup string
	}{
		{"foreign account", 9000000001, float64(18), `{"response":{"users":[{"id":17,"telegramId":9000000001}]}}`},
		{"foreign Telegram owner", 9000000001, float64(17), `{"response":{"users":[{"id":17,"telegramId":9000000002}]}}`},
		{"foreign account beside owned account", 9000000001, float64(17), `{"response":{"users":[{"id":17,"telegramId":9000000002},{"id":18,"telegramId":9000000001}]}}`},
		{"adjacent Telegram IDs retain precision", 9007199254740992, float64(17), `{"response":{"users":[{"id":17,"telegramId":9007199254740993}]}}`},
		{"lookup failure", 9000000001, float64(17), `{"ok":false,"response":{"users":[{"id":17,"telegramId":9000000001}]}}`},
		{"malformed lookup", 9000000001, float64(17), `{"response":`},
		{"trailing JSON", 9000000001, float64(17), `{"response":{"users":[{"id":17,"telegramId":9000000001}]}} {}`},
		{"fractional Telegram ID", 9000000001, float64(17), `{"response":{"users":[{"id":17,"telegramId":9000000001.5}]}}`},
		{"fractional account ID", 9000000001, float64(17.5), `{"response":{"users":[{"id":17.5,"telegramId":9000000001}]}}`},
		{"boolean account ID", 9000000001, "true", `{"response":{"users":[{"id":true,"telegramId":9000000001}]}}`},
		{"rounded model account ID", 9000000001, float64(9007199254740993), `{"response":{"users":[{"id":9007199254740993,"telegramId":9000000001}]}}`},
	} {
		for _, tool := range []string{"users_get", "hwid_device_delete"} {
			t.Run(tc.name+"/"+tool, func(t *testing.T) {
				rem := &mockClient{name: "remnawave", tools: []Tool{{Name: tool, InputSchema: schema(map[string]string{"userId": "number", "telegramId": "number"})}}, results: map[string]string{"users_get_by_telegram_id": tc.lookup}}
				result, err := NewRouter([]Client{rem}, false).CallTool(context.Background(), tool, map[string]any{"userId": tc.wanted, "telegramId": float64(9000000002), "hwid": "foreign-device"}, tc.key)
				if err != nil || !contains(result, "ownership_") || contains(result, "identity_unavailable") {
					t.Fatalf("unverified %s accepted: %s %v", tool, result, err)
				}
				for index, call := range rem.calls {
					if call != "users_get_by_telegram_id" || rem.args[index]["telegramId"] != tc.key {
						t.Fatalf("unverified account reached upstream or lookup not pinned: %v %v", rem.calls, rem.args)
					}
				}
			})
		}
	}
}

func TestOwnershipErrorsDescribeActualFailure(t *testing.T) {
	for _, tc := range []struct {
		name, lookup, code string
		err                error
		retryable          bool
	}{
		{name: "foreign account", lookup: `{"response":{"users":[{"id":18,"telegramId":42}]}}`, code: "ownership_unverified"},
		{name: "empty account list", lookup: `{"response":{"users":[]}}`, code: "ownership_unverified"},
		{name: "transport failure", err: errors.New("timeout"), code: "ownership_check_unavailable", retryable: true},
		{name: "tool error", lookup: `{"error":"upstream unavailable"}`, code: "ownership_check_unavailable", retryable: true},
		{name: "failed envelope", lookup: `{"ok":false,"response":{"users":[{"id":17,"telegramId":42}]}}`, code: "ownership_check_unavailable", retryable: true},
		{name: "nested error", lookup: `{"response":{"error":"upstream unavailable","users":[]}}`, code: "ownership_check_unavailable", retryable: true},
		{name: "malformed response", lookup: `{"response":`, code: "ownership_check_unavailable", retryable: true},
		{name: "unexpected shape", lookup: `{"ok":true}`, code: "ownership_check_unavailable", retryable: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rem := &mockClient{name: "remnawave", tools: []Tool{{Name: "hwid_device_delete", InputSchema: schema(map[string]string{"userId": "number", "telegramId": "number"})}}, results: map[string]string{"users_get_by_telegram_id": tc.lookup}, err: tc.err}
			result, err := NewRouter([]Client{rem}, false).CallTool(context.Background(), "hwid_device_delete", map[string]any{"userId": 17, "hwid": "device"}, 42)
			if err != nil {
				t.Fatal(err)
			}
			var envelope struct {
				Error struct {
					Code, Message string
					Retryable     bool
				}
			}
			if err := json.Unmarshal([]byte(result), &envelope); err != nil {
				t.Fatal(err)
			}
			if envelope.Error.Code != tc.code || envelope.Error.Retryable != tc.retryable || contains(result, "identity_unavailable") {
				t.Fatalf("incorrect error: %s", result)
			}
			if !tc.retryable && !contains(envelope.Error.Message, "список устройств") {
				t.Fatalf("missing recovery instruction: %s", result)
			}
			if len(rem.calls) != 1 || rem.calls[0] != "users_get_by_telegram_id" {
				t.Fatalf("unverified deletion reached upstream: %v", rem.calls)
			}
		})
	}
}

func TestOwnedUUIDWithLargeTelegramID(t *testing.T) {
	rem := &mockClient{name: "remnawave", tools: []Tool{{Name: "users_get", InputSchema: schema(map[string]string{"userId": "string"})}}, results: map[string]string{
		"users_get_by_telegram_id": `{"response":{"users":[{"uuid":"owner-uuid","telegramId":9000000001}]}}`,
		"users_get":                `{"response":{"uuid":"owner-uuid"}}`,
	}}
	result, err := NewRouter([]Client{rem}, false).CallTool(context.Background(), "users_get", map[string]any{"userId": "owner-uuid"}, 9000000001)
	if err != nil || result != rem.results["users_get"] || len(rem.calls) != 2 {
		t.Fatalf("owned UUID rejected: %s %v %v", result, err, rem.calls)
	}
}
