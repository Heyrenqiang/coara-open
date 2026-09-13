package db

import (
	"database/sql"
	"encoding/json"

	"gomatrix/internal/models"
)

// GetToDeviceMessages returns pending to-device messages and optionally deletes them.
// The read and delete run inside a single transaction so concurrent syncs
// cannot observe and re-deliver the same messages.
func (db *DB) GetToDeviceMessages(userID, deviceID string, delete bool) ([]*models.ToDeviceMessage, error) {
	var msgs []*models.ToDeviceMessage
	err := db.InTransaction(func(tx *sql.Tx) error {
		msgs = nil
		rows, err := tx.Query(
			`SELECT id, user_id, device_id, type, content, message_id FROM to_device
			 WHERE user_id = ? AND device_id = ? ORDER BY id ASC`,
			userID, deviceID,
		)
		if err != nil {
			return err
		}

		var ids []int64
		for rows.Next() {
			m := &models.ToDeviceMessage{}
			var content string
			if err := rows.Scan(&m.ID, &m.UserID, &m.DeviceID, &m.Type, &content, &m.MessageID); err != nil {
				rows.Close()
				return err
			}
			m.Content = json.RawMessage(content)
			msgs = append(msgs, m)
			ids = append(ids, m.ID)
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		rows.Close()

		if delete && len(ids) > 0 {
			query := `DELETE FROM to_device WHERE id IN (` + int64Placeholders(len(ids)) + `)`
			args := make([]any, len(ids))
			for i, id := range ids {
				args[i] = id
			}
			if _, err := tx.Exec(query, args...); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return msgs, nil
}

// InsertToDeviceMessage queues a to-device message.
func (db *DB) InsertToDeviceMessage(userID, deviceID, msgType string, content json.RawMessage, messageID string) error {
	_, err := db.Exec(
		`INSERT INTO to_device (user_id, device_id, type, content, message_id) VALUES (?, ?, ?, ?, ?)`,
		userID, deviceID, msgType, string(content), messageID,
	)
	return err
}

// GetAccountDataByType is a convenience wrapper for typed account data.
func (db *DB) GetAccountDataByType(userID string, roomID *string, dataType string) (json.RawMessage, error) {
	return db.GetAccountData(userID, roomID, dataType)
}

func int64Placeholders(n int) string {
	if n <= 0 {
		return ""
	}
	s := "?"
	for i := 1; i < n; i++ {
		s += ",?"
	}
	return s
}
