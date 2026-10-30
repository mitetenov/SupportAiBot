package storage

import (
	"context"
	"testing"
	"time"
)

func TestDeviceSnapshotSurvivesHistoryTrimmingAndStaysPrivate(t *testing.T) {
	ctx := context.Background()
	history := NewHistory(nil, 2, 7)
	if err := history.SetDeviceSnapshot(ctx, 42, `{"userId":17,"hwid":"private-device"}`); err != nil {
		t.Fatal(err)
	}
	for _, text := range []string{"first", "second", "third"} {
		if err := history.AddUserMessage(ctx, 42, text); err != nil {
			t.Fatal(err)
		}
	}
	if snapshot, err := history.GetDeviceSnapshot(ctx, 42); err != nil || snapshot != `{"userId":17,"hwid":"private-device"}` {
		t.Fatalf("lost device evidence: %s %v", snapshot, err)
	}
	if snapshot, err := history.GetDeviceSnapshot(ctx, 43); err != nil || snapshot != "" {
		t.Fatalf("cross-user device evidence: %s %v", snapshot, err)
	}
	messages, _ := history.GetHistory(ctx, 42)
	if len(messages) != 2 || messages[0].Content != "second" || messages[1].Content != "third" {
		t.Fatalf("device evidence leaked into text history: %v", messages)
	}
	if err := history.SetDeviceSnapshot(ctx, 43, "other"); err != nil {
		t.Fatal(err)
	}
	if err := history.Clear(ctx, 42); err != nil {
		t.Fatal(err)
	}
	if snapshot, _ := history.GetDeviceSnapshot(ctx, 42); snapshot != "" {
		t.Fatal("cleared conversation retained device evidence")
	}
	if snapshot, _ := history.GetDeviceSnapshot(ctx, 43); snapshot != "other" {
		t.Fatal("clearing one conversation removed another user's evidence")
	}
}

func TestDeviceSnapshotExpiryAndInvalidation(t *testing.T) {
	ctx := context.Background()
	for _, cleanup := range []bool{false, true} {
		history := NewHistory(nil, 20, 7)
		history.devices[42] = deviceSnapshot{text: "stale", at: time.Now().Add(-8 * 24 * time.Hour)}
		// Recent conversation activity must not extend the device snapshot's age.
		if _, err := history.GetHistory(ctx, 42); err != nil {
			t.Fatal(err)
		}
		if cleanup {
			if err := history.EvictStaleEntries(ctx); err != nil {
				t.Fatal(err)
			}
		}
		if snapshot, err := history.GetDeviceSnapshot(ctx, 42); err != nil || snapshot != "" {
			t.Fatalf("expired device evidence reused: %s %v", snapshot, err)
		}
	}
	history := NewHistory(nil, 20, 7)
	if err := history.SetDeviceSnapshot(ctx, 42, "current"); err != nil {
		t.Fatal(err)
	}
	if err := history.SetDeviceSnapshot(ctx, 42, ""); err != nil {
		t.Fatal(err)
	}
	if snapshot, _ := history.GetDeviceSnapshot(ctx, 42); snapshot != "" {
		t.Fatal("invalidated device evidence reused")
	}
}
