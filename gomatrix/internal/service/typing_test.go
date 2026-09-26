package service

import (
	"encoding/json"
	"testing"
	"time"
)

func decodeUserIDs(t *testing.T, raw json.RawMessage) []string {
	t.Helper()
	var ev struct {
		Type    string `json:"type"`
		Content struct {
			UserIDs []string `json:"user_ids"`
		} `json:"content"`
	}
	if err := json.Unmarshal(raw, &ev); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if ev.Type != "m.typing" {
		t.Fatalf("type=%q", ev.Type)
	}
	if ev.Content.UserIDs == nil {
		return []string{}
	}
	return ev.Content.UserIDs
}

func TestTypingEphemeralEvents(t *testing.T) {
	ts := NewTypingService()
	ts.SetTyping("!room:test.local", "@alice:test.local", true, 30_000)
	ts.SetTyping("!room:test.local", "@bob:test.local", true, 30_000)

	events := ts.EphemeralEvents("!room:test.local", "@alice:test.local")
	if len(events) != 1 {
		t.Fatalf("got %d events, want 1", len(events))
	}
	ids := decodeUserIDs(t, events[0])
	if len(ids) != 2 {
		t.Fatalf("want both typers in user_ids, got %v", ids)
	}

	if got := ts.EphemeralEvents("!other:test.local", ""); got != nil {
		t.Fatalf("expected nil for unknown room, got %v", got)
	}

	ts.SetTyping("!room:test.local", "@bob:test.local", false, 0)
	got := ts.EphemeralEvents("!room:test.local", "")
	if len(got) != 1 {
		t.Fatalf("got %d events after stop, want 1", len(got))
	}
	ids = decodeUserIDs(t, got[0])
	if len(ids) != 1 || ids[0] != "@alice:test.local" {
		t.Fatalf("want alice still typing, got %v", ids)
	}
}

func TestTypingClearRebroadcastForAllClients(t *testing.T) {
	ts := NewTypingService()
	ts.SetTyping("!room:test.local", "@bob:test.local", true, 30_000)
	_ = ts.EphemeralEvents("!room:test.local", "") // bot sync

	ts.SetTyping("!room:test.local", "@bob:test.local", false, 0)

	// Bot sync must not consume the clear — phone sync still needs it.
	botView := ts.EphemeralEvents("!room:test.local", "@bob:test.local")
	if len(botView) != 1 || len(decodeUserIDs(t, botView[0])) != 0 {
		t.Fatalf("bot clear snapshot: %v", botView)
	}
	phoneView := ts.EphemeralEvents("!room:test.local", "@phone:test.local")
	if len(phoneView) != 1 || len(decodeUserIDs(t, phoneView[0])) != 0 {
		t.Fatalf("phone clear snapshot: %v", phoneView)
	}
	// Still re-advertised within TTL.
	again := ts.EphemeralEvents("!room:test.local", "@phone:test.local")
	if len(again) != 1 || len(decodeUserIDs(t, again[0])) != 0 {
		t.Fatalf("expected rebroadcast clear, got %v", again)
	}
}

func TestTypingEphemeralEventsExpires(t *testing.T) {
	ts := NewTypingService()
	ts.SetTyping("!room:test.local", "@alice:test.local", true, 1) // 1ms timeout
	time.Sleep(10 * time.Millisecond)

	got := ts.EphemeralEvents("!room:test.local", "")
	if len(got) != 1 || len(decodeUserIDs(t, got[0])) != 0 {
		t.Fatalf("want clear snapshot after expiry, got %v", got)
	}

	ts.mu.RLock()
	_, ok := ts.rooms["!room:test.local"]
	ts.mu.RUnlock()
	if ok {
		t.Fatal("expired room entry was not pruned")
	}
}
