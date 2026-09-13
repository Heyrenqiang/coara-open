package db

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"sync"
	"testing"

	"gomatrix/internal/config"
)

func openTestDB(t *testing.T) *DB {
	t.Helper()
	cfg := &config.Config{
		ServerName:   "test.local",
		DatabasePath: filepath.Join(t.TempDir(), "test.db"),
	}
	db, err := Open(cfg)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func insertToDevice(t *testing.T, db *DB, userID, deviceID string, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		content, _ := json.Marshal(map[string]int{"n": i})
		if err := db.InsertToDeviceMessage(userID, deviceID, "m.test", content, fmt.Sprintf("m%d", i)); err != nil {
			t.Fatalf("insert to-device: %v", err)
		}
	}
}

func TestGetToDeviceMessagesDelete(t *testing.T) {
	db := openTestDB(t)
	insertToDevice(t, db, "@alice:test.local", "dev1", 3)

	// delete=false must not consume the messages.
	msgs, err := db.GetToDeviceMessages("@alice:test.local", "dev1", false)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if len(msgs) != 3 {
		t.Fatalf("got %d messages, want 3", len(msgs))
	}
	msgs, err = db.GetToDeviceMessages("@alice:test.local", "dev1", false)
	if err != nil {
		t.Fatalf("get again: %v", err)
	}
	if len(msgs) != 3 {
		t.Fatalf("delete=false consumed messages: got %d, want 3", len(msgs))
	}

	// delete=true consumes exactly once.
	msgs, err = db.GetToDeviceMessages("@alice:test.local", "dev1", true)
	if err != nil {
		t.Fatalf("get delete: %v", err)
	}
	if len(msgs) != 3 {
		t.Fatalf("got %d messages, want 3", len(msgs))
	}
	msgs, err = db.GetToDeviceMessages("@alice:test.local", "dev1", true)
	if err != nil {
		t.Fatalf("get after delete: %v", err)
	}
	if len(msgs) != 0 {
		t.Fatalf("messages re-delivered after delete: got %d, want 0", len(msgs))
	}
}

func TestGetToDeviceMessagesConcurrentNoDoubleDelivery(t *testing.T) {
	db := openTestDB(t)
	const total = 50
	insertToDevice(t, db, "@alice:test.local", "dev1", total)

	var mu sync.Mutex
	seen := make(map[int64]string) // row id -> message_id
	var wg sync.WaitGroup
	for w := 0; w < 8; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for attempt := 0; attempt < 10; attempt++ {
				msgs, err := db.GetToDeviceMessages("@alice:test.local", "dev1", true)
				if err != nil {
					t.Errorf("get: %v", err)
					return
				}
				if len(msgs) == 0 {
					return
				}
				mu.Lock()
				for _, m := range msgs {
					if prev, dup := seen[m.ID]; dup {
						t.Errorf("message %d re-delivered (message_id %s and %s)", m.ID, prev, m.MessageID)
					}
					seen[m.ID] = m.MessageID
				}
				mu.Unlock()
			}
		}()
	}
	wg.Wait()

	if len(seen) != total {
		t.Fatalf("delivered %d unique messages, want %d", len(seen), total)
	}
}
