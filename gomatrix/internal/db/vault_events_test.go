package db

import (
	"database/sql"
	"encoding/json"
	"testing"

	"gomatrix/internal/models"
)

// 插入一条 m.room.message 事件，返回事件 ID。
func insertMessageEvent(t *testing.T, db *DB, eventID, body string, ts int64) {
	t.Helper()
	if _, err := db.Exec(
		`INSERT OR IGNORE INTO rooms (room_id, version, creator, join_rules, created_at)
		 VALUES ('!r:test.local', '1', '@u:test.local', 'invite', 0)`,
	); err != nil {
		t.Fatalf("insert room: %v", err)
	}
	content, _ := json.Marshal(map[string]string{"msgtype": "m.text", "body": body})
	ev := &models.Event{
		EventID:        eventID,
		RoomID:         "!r:test.local",
		Sender:         "@u:test.local",
		Type:           "m.room.message",
		Content:        content,
		OriginServerTs: ts,
		Depth:          1,
		Unsigned:       json.RawMessage("{}"),
	}
	if err := db.InTransaction(func(tx *sql.Tx) error {
		_, err := db.SaveEvent(tx, ev)
		return err
	}); err != nil {
		t.Fatalf("save event: %v", err)
	}
}

func TestDeleteVaultReplyEvents(t *testing.T) {
	db := openTestDB(t)
	const cutoff = 1000

	// 超窗回信：应删
	insertMessageEvent(t, db, "$old-reply", "[COARA_VAULT_REPLY]secret[/COARA_VAULT_REPLY]", 500)
	// 窗内回信：应保留（仍在投递窗口）
	insertMessageEvent(t, db, "$fresh-reply", "[COARA_VAULT_REPLY]secret[/COARA_VAULT_REPLY]", 2000)
	// 超窗但非回信：应保留
	insertMessageEvent(t, db, "$old-plain", "[COARA_VAULT] prompt without password", 500)
	insertMessageEvent(t, db, "$old-chat", "hello", 500)

	n, err := db.DeleteVaultReplyEvents(cutoff)
	if err != nil {
		t.Fatalf("delete: %v", err)
	}
	if n != 1 {
		t.Fatalf("deleted %d events, want 1", n)
	}

	if ev, _ := db.GetEvent("$old-reply"); ev != nil {
		t.Fatal("old vault reply still present")
	}
	for _, id := range []string{"$fresh-reply", "$old-plain", "$old-chat"} {
		if ev, _ := db.GetEvent(id); ev == nil {
			t.Fatalf("event %s wrongly deleted", id)
		}
	}
	// timeline 行应级联删除
	var cnt int
	if err := db.QueryRow(`SELECT COUNT(*) FROM timeline WHERE event_id = '$old-reply'`).Scan(&cnt); err != nil {
		t.Fatalf("count timeline: %v", err)
	}
	if cnt != 0 {
		t.Fatal("timeline row for deleted vault reply still present")
	}
}
