package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"gomatrix/internal/config"
	"gomatrix/internal/db"
)

func setupTestServer(t *testing.T) (*Services, func()) {
	t.Helper()
	dir := t.TempDir()
	cfg := &config.Config{
		ServerName:         "test.local",
		DatabasePath:       filepath.Join(dir, "test.db"),
		Address:            "127.0.0.1",
		Port:               0,
		MaxRequestSize:     1024 * 1024,
		AllowRegistration:  true,
		DefaultRoomVersion: "10",
		MediaPath:          filepath.Join(dir, "media"),
		LogLevel:           "error",
	}
	database, err := db.Open(cfg)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	s := Build(cfg, database)
	SetGlobal(s)
	return s, func() {
		database.Close()
		_ = os.RemoveAll(dir)
	}
}

func TestRegisterAndLogin(t *testing.T) {
	s, cleanup := setupTestServer(t)
	defer cleanup()

	userID, err := s.Users.Register("alice", "secret", false)
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	if userID != "@alice:test.local" {
		t.Fatalf("unexpected user id: %s", userID)
	}

	uid, token, deviceID, err := s.Users.Login("alice", "secret", "", "dev")
	if err != nil {
		t.Fatalf("login: %v", err)
	}
	if uid != userID {
		t.Fatalf("unexpected uid: %s", uid)
	}
	if token == "" || deviceID == "" {
		t.Fatal("expected token and device id")
	}
}

func TestCreateRoomWithInvite(t *testing.T) {
	s, cleanup := setupTestServer(t)
	defer cleanup()

	aliceID, err := s.Users.Register("alice", "secret", false)
	if err != nil {
		t.Fatalf("register alice: %v", err)
	}
	botID, err := s.Users.Register("gora", "secret", false)
	if err != nil {
		t.Fatalf("register bot: %v", err)
	}

	room, err := s.Rooms.CreateRoom(aliceID, &CreateRoomRequest{
		Invite: []string{botID},
		Preset: "trusted_private_chat",
	})
	if err != nil {
		t.Fatalf("create room with invite: %v", err)
	}
	if room.RoomID == "" {
		t.Fatal("expected room id")
	}
	m, err := s.DB.GetMembership(room.RoomID, botID)
	if err != nil {
		t.Fatalf("get bot membership: %v", err)
	}
	if m != "invite" {
		t.Fatalf("bot membership = %q, want invite", m)
	}
}

func TestCreateRoomAndSendMessage(t *testing.T) {
	s, cleanup := setupTestServer(t)
	defer cleanup()

	aliceID, err := s.Users.Register("alice", "secret", false)
	if err != nil {
		t.Fatalf("register alice: %v", err)
	}

	room, err := s.Rooms.CreateRoom(aliceID, &CreateRoomRequest{
		Name:   "Test Room",
		Preset: "public_chat",
	})
	if err != nil {
		t.Fatalf("create room: %v", err)
	}
	if room.RoomID == "" {
		t.Fatal("expected room id")
	}

	content, _ := json.Marshal(map[string]string{
		"msgtype": "m.text",
		"body":    "hello",
	})
	ev, err := s.Timeline.SendMessage(room.RoomID, aliceID, "m.room.message", content)
	if err != nil {
		t.Fatalf("send message: %v", err)
	}
	if ev.EventID == "" {
		t.Fatal("expected event id")
	}
}

func TestSync(t *testing.T) {
	s, cleanup := setupTestServer(t)
	defer cleanup()

	aliceID, _ := s.Users.Register("alice", "secret", false)
	room, _ := s.Rooms.CreateRoom(aliceID, &CreateRoomRequest{Name: "Sync Test"})
	content, _ := json.Marshal(map[string]string{"msgtype": "m.text", "body": "sync me"})
	s.Timeline.SendMessage(room.RoomID, aliceID, "m.room.message", content)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	resp, err := s.Sync.Sync(ctx, aliceID, "dev1", "", 0)
	if err != nil {
		t.Fatalf("sync: %v", err)
	}
	if resp.NextBatch == "" {
		t.Fatal("expected next_batch")
	}
	jr, ok := resp.Rooms.Join[room.RoomID]
	if !ok {
		t.Fatal("expected joined room in sync")
	}
	// Initial sync must NOT dump historic timeline (bot replay hazard).
	if len(jr.Timeline.Events) != 0 {
		t.Fatalf("initial sync timeline must be empty, got %d events", len(jr.Timeline.Events))
	}
	if len(jr.State.Events) == 0 {
		t.Fatal("expected state events on initial sync")
	}

	// Incremental sync from s0 still delivers the message.
	inc, err := s.Sync.Sync(ctx, aliceID, "dev1", "s0", 0)
	if err != nil {
		t.Fatalf("incremental sync: %v", err)
	}
	incRoom, ok := inc.Rooms.Join[room.RoomID]
	if !ok || len(incRoom.Timeline.Events) == 0 {
		t.Fatal("expected timeline events on incremental sync from s0")
	}

	raw, err := json.Marshal(resp)
	if err != nil {
		t.Fatalf("marshal sync: %v", err)
	}
	var parsed map[string]any
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatalf("unmarshal sync: %v", err)
	}
	accountEvents := parsed["account_data"].(map[string]any)["events"]
	if accountEvents == nil {
		t.Fatal("account_data.events must be [] not null for matrix-nio")
	}
	joinRoom := parsed["rooms"].(map[string]any)["join"].(map[string]any)[room.RoomID].(map[string]any)
	if joinRoom["account_data"].(map[string]any)["events"] == nil {
		t.Fatal("joined room account_data.events must be [] not null for matrix-nio")
	}
	if joinRoom["ephemeral"].(map[string]any)["events"] == nil {
		t.Fatal("joined room ephemeral.events must be [] not null for matrix-nio")
	}
}

func TestInitialSyncOmitsHistoricTimeline(t *testing.T) {
	s, cleanup := setupTestServer(t)
	defer cleanup()

	aliceID, _ := s.Users.Register("alice", "secret", false)
	room, _ := s.Rooms.CreateRoom(aliceID, &CreateRoomRequest{Name: "History Dump Guard"})
	for i := 0; i < 50; i++ {
		content, _ := json.Marshal(map[string]string{
			"msgtype": "m.text",
			"body":    fmt.Sprintf("msg-%d", i),
		})
		if _, err := s.Timeline.SendMessage(room.RoomID, aliceID, "m.room.message", content); err != nil {
			t.Fatalf("send %d: %v", i, err)
		}
	}

	ctx := context.Background()
	initial, err := s.Sync.Sync(ctx, aliceID, "dev1", "", 0)
	if err != nil {
		t.Fatalf("initial sync: %v", err)
	}
	jr := initial.Rooms.Join[room.RoomID]
	if jr == nil {
		t.Fatal("expected joined room")
	}
	if len(jr.Timeline.Events) != 0 {
		t.Fatalf("initial sync must not dump history, got %d timeline events", len(jr.Timeline.Events))
	}
	if initial.NextBatch == "" || initial.NextBatch == "s0" {
		t.Fatalf("expected tip next_batch, got %q", initial.NextBatch)
	}

	// A fresh incremental from the parked tip must be empty.
	tip, err := s.Sync.Sync(ctx, aliceID, "dev1", initial.NextBatch, 0)
	if err != nil {
		t.Fatalf("tip sync: %v", err)
	}
	if tipRoom := tip.Rooms.Join[room.RoomID]; tipRoom != nil && len(tipRoom.Timeline.Events) != 0 {
		t.Fatalf("sync from tip must be empty, got %d events", len(tipRoom.Timeline.Events))
	}
}

func TestSyncInvalidToken(t *testing.T) {
	s, cleanup := setupTestServer(t)
	defer cleanup()

	aliceID, _ := s.Users.Register("alice", "secret", false)
	ctx := context.Background()
	_, err := s.Sync.Sync(ctx, aliceID, "dev1", "42648", 0)
	if !errors.Is(err, ErrInvalidSyncToken) {
		t.Fatalf("expected ErrInvalidSyncToken, got %v", err)
	}
}

func TestSyncTimeoutKeepsToken(t *testing.T) {
	s, cleanup := setupTestServer(t)
	defer cleanup()

	aliceID, _ := s.Users.Register("alice", "secret", false)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	since := "s0"
	resp, err := s.Sync.Sync(ctx, aliceID, "dev1", since, 200*time.Millisecond)
	if err != nil {
		t.Fatalf("sync: %v", err)
	}
	if resp.NextBatch != since {
		t.Fatalf("timeout next_batch = %q, want %q", resp.NextBatch, since)
	}
}

func TestSyncDeliversMessageAfterLongPoll(t *testing.T) {
	s, cleanup := setupTestServer(t)
	defer cleanup()

	aliceID, _ := s.Users.Register("alice", "secret", false)
	bobID, _ := s.Users.Register("bob", "secret", false)
	room, _ := s.Rooms.CreateRoom(aliceID, &CreateRoomRequest{Name: "Chat", Invite: []string{bobID}})
	_ = s.Rooms.JoinRoom(room.RoomID, bobID)

	ctx := context.Background()
	initial, err := s.Sync.Sync(ctx, bobID, "dev1", "", 0)
	if err != nil {
		t.Fatalf("initial sync: %v", err)
	}
	since := initial.NextBatch

	done := make(chan error, 1)
	go func() {
		resp, syncErr := s.Sync.Sync(ctx, bobID, "dev1", since, 2*time.Second)
		if syncErr != nil {
			done <- syncErr
			return
		}
		jr := resp.Rooms.Join[room.RoomID]
		if jr == nil || len(jr.Timeline.Events) == 0 {
			done <- fmt.Errorf("expected reply in sync, next_batch=%s", resp.NextBatch)
			return
		}
		done <- nil
	}()

	time.Sleep(100 * time.Millisecond)
	content, _ := json.Marshal(map[string]string{"msgtype": "m.text", "body": "reply from coara"})
	if _, err := s.Timeline.SendMessage(room.RoomID, aliceID, "m.room.message", content); err != nil {
		t.Fatalf("send: %v", err)
	}

	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("sync long poll did not return after message send")
	}
}

func TestSyncDeliversTimelineToInvitedBot(t *testing.T) {
	s, cleanup := setupTestServer(t)
	defer cleanup()

	aliceID, _ := s.Users.Register("alice", "secret", false)
	botID, _ := s.Users.Register("coara", "secret", false)
	room, _ := s.Rooms.CreateRoom(aliceID, &CreateRoomRequest{
		Name:   "Phone Chat",
		Invite: []string{botID},
		Preset: "trusted_private_chat",
	})

	ctx := context.Background()
	initial, err := s.Sync.Sync(ctx, botID, "dev1", "", 0)
	if err != nil {
		t.Fatalf("bot initial sync: %v", err)
	}
	since := initial.NextBatch

	content, _ := json.Marshal(map[string]string{"msgtype": "m.text", "body": "from phone"})
	if _, err := s.Timeline.SendMessage(room.RoomID, aliceID, "m.room.message", content); err != nil {
		t.Fatalf("send: %v", err)
	}

	resp, err := s.Sync.Sync(ctx, botID, "dev1", since, 0)
	if err != nil {
		t.Fatalf("incremental sync: %v", err)
	}
	jr := resp.Rooms.Join[room.RoomID]
	if jr == nil || len(jr.Timeline.Events) == 0 {
		t.Fatalf("invited bot must receive timeline in rooms.join, next_batch=%s", resp.NextBatch)
	}
	if resp.NextBatch == since {
		t.Fatalf("next_batch must advance after invite-room delivery, still %s", since)
	}
	if _, ok := resp.Rooms.Invite[room.RoomID]; !ok {
		t.Fatal("expected invite block while bot has not joined")
	}
}

func TestSyncDoesNotSkipUndeliveredEvents(t *testing.T) {
	s, cleanup := setupTestServer(t)
	defer cleanup()

	aliceID, _ := s.Users.Register("alice", "secret", false)
	bobID, _ := s.Users.Register("bob", "secret", false)
	room, _ := s.Rooms.CreateRoom(aliceID, &CreateRoomRequest{Name: "Skip Test", Invite: []string{bobID}})
	_ = s.Rooms.JoinRoom(room.RoomID, bobID)

	ctx := context.Background()
	initial, err := s.Sync.Sync(ctx, bobID, "dev1", "", 0)
	if err != nil {
		t.Fatalf("initial sync: %v", err)
	}
	since := initial.NextBatch

	content, _ := json.Marshal(map[string]string{"msgtype": "m.text", "body": "must arrive"})
	if _, err := s.Timeline.SendMessage(room.RoomID, aliceID, "m.room.message", content); err != nil {
		t.Fatalf("send: %v", err)
	}

	resp, err := s.Sync.Sync(ctx, bobID, "dev1", since, 0)
	if err != nil {
		t.Fatalf("incremental sync: %v", err)
	}
	jr := resp.Rooms.Join[room.RoomID]
	if jr == nil || len(jr.Timeline.Events) == 0 {
		t.Fatalf("expected timeline delivery, next_batch=%s", resp.NextBatch)
	}
	if resp.NextBatch == since {
		t.Fatalf("next_batch should advance after delivery, still %s", since)
	}
}

func TestRegistrationTicketIsSingleUse(t *testing.T) {
	tickets := newRegistrationTickets()
	ticket, err := tickets.Issue()
	if err != nil {
		t.Fatalf("Issue() error = %v", err)
	}
	if !tickets.Consume(ticket) {
		t.Fatal("first Consume() = false, want true")
	}
	if tickets.Consume(ticket) {
		t.Fatal("second Consume() = true, want false")
	}
}

func TestRegistrationTicketExpires(t *testing.T) {
	tickets := newRegistrationTickets()
	tickets.tickets["expired"] = time.Now().Add(-time.Second)
	if tickets.Consume("expired") {
		t.Fatal("Consume(expired) = true, want false")
	}
}

func TestSendMessageWithTxnDedupesConcurrentRetries(t *testing.T) {
	s, cleanup := setupTestServer(t)
	defer cleanup()

	aliceID, err := s.Users.Register("alice", "secret", false)
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	room, err := s.Rooms.CreateRoom(aliceID, &CreateRoomRequest{Preset: "private_chat"})
	if err != nil {
		t.Fatalf("create room: %v", err)
	}

	const txnID = "m123456.1"
	content := json.RawMessage(`{"msgtype":"m.text","body":"hello"}`)
	const workers = 8
	ids := make(chan string, workers)
	var wg sync.WaitGroup
	wg.Add(workers)
	for i := 0; i < workers; i++ {
		go func() {
			defer wg.Done()
			ev, err := s.Timeline.SendMessageWithTxn(room.RoomID, aliceID, "m.room.message", content, txnID)
			if err != nil {
				t.Errorf("SendMessageWithTxn: %v", err)
				return
			}
			ids <- ev.EventID
		}()
	}
	wg.Wait()
	close(ids)

	seen := map[string]struct{}{}
	for id := range ids {
		seen[id] = struct{}{}
	}
	if len(seen) != 1 {
		t.Fatalf("expected 1 unique event id for same txn, got %d (%v)", len(seen), seen)
	}
}
