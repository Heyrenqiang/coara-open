package service

import (
	"encoding/json"
	"sync"
	"time"
)

// How long to keep re-advertising the latest typing snapshot after a change.
// Must cover multiple clients' sync cycles: a one-shot clear is often consumed by
// the bot's own /sync, so the phone never sees user_ids:[] and stays on "typing…".
const typingBroadcastTTL = 45 * time.Second

// TypingService tracks ephemeral m.typing state per room.
type TypingService struct {
	mu sync.RWMutex
	// roomID -> userID -> expiresAt
	rooms map[string]map[string]time.Time
	// roomID -> re-broadcast deadline for the current snapshot (incl. empty).
	broadcastUntil map[string]time.Time
}

// NewTypingService creates a typing tracker.
func NewTypingService() *TypingService {
	return &TypingService{
		rooms:          make(map[string]map[string]time.Time),
		broadcastUntil: make(map[string]time.Time),
	}
}

func (ts *TypingService) markBroadcast(roomID string) {
	ts.broadcastUntil[roomID] = time.Now().Add(typingBroadcastTTL)
}

// SetTyping updates typing state for a user in a room.
func (ts *TypingService) SetTyping(roomID, userID string, typing bool, timeoutMS int64) {
	ts.mu.Lock()
	defer ts.mu.Unlock()
	if !typing {
		if users, ok := ts.rooms[roomID]; ok {
			delete(users, userID)
			if len(users) == 0 {
				delete(ts.rooms, roomID)
			}
		}
		ts.markBroadcast(roomID)
		return
	}
	if timeoutMS <= 0 {
		timeoutMS = 30_000
	}
	if ts.rooms[roomID] == nil {
		ts.rooms[roomID] = make(map[string]time.Time)
	}
	ts.rooms[roomID][userID] = time.Now().Add(time.Duration(timeoutMS) * time.Millisecond)
	ts.markBroadcast(roomID)
}

// EphemeralEvents returns m.typing JSON events for a room.
//
// Matrix uses a single m.typing event whose content.user_ids lists everyone
// currently typing (callers may ignore their own id). excludeUser is kept for
// API compatibility but is not filtered out of user_ids — filtering made the
// sole typer's own /sync see user_ids:[] while still typing.
//
// After stop/expiry, the empty snapshot is re-advertised for typingBroadcastTTL
// so every syncing client (phone + bot) can clear "typing…".
func (ts *TypingService) EphemeralEvents(roomID, excludeUser string) []json.RawMessage {
	_ = excludeUser
	now := time.Now()

	ts.mu.Lock()
	defer ts.mu.Unlock()

	users := ts.rooms[roomID]
	live := make([]string, 0, 4)
	if users != nil {
		for userID, expires := range users {
			if !expires.After(now) {
				delete(users, userID)
				ts.markBroadcast(roomID)
				continue
			}
			live = append(live, userID)
		}
		if len(users) == 0 {
			delete(ts.rooms, roomID)
		}
	}

	until, pending := ts.broadcastUntil[roomID]
	if pending && !until.After(now) {
		delete(ts.broadcastUntil, roomID)
		pending = false
	}

	// Active typers: always include snapshot. Pending clear/start: re-advertise
	// until TTL (do NOT consume on read — all clients must see it).
	if !pending && len(live) == 0 {
		return nil
	}

	payload, _ := json.Marshal(map[string]any{
		"type": "m.typing",
		"content": map[string]any{
			"user_ids": live,
		},
	})
	return []json.RawMessage{payload}
}
