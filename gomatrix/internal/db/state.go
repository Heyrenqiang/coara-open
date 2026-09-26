package db

import (
	"database/sql"
	"encoding/json"

	"gomatrix/internal/models"
)

// SetStateEvent upserts a current state event for a room.
func (db *DB) SetStateEvent(tx *sql.Tx, roomID, eventType, stateKey, eventID string) error {
	_, err := tx.Exec(
		`INSERT INTO state_events (room_id, event_type, state_key, event_id)
		 VALUES (?, ?, ?, ?)
		 ON CONFLICT(room_id, event_type, state_key) DO UPDATE SET event_id=excluded.event_id`,
		roomID, eventType, stateKey, eventID,
	)
	return err
}

// GetStateEvent returns the current event ID for a state tuple.
func (db *DB) GetStateEvent(roomID, eventType, stateKey string) (string, error) {
	row := db.QueryRow(
		`SELECT event_id FROM state_events WHERE room_id = ? AND event_type = ? AND state_key = ?`,
		roomID, eventType, stateKey,
	)
	var eventID string
	err := row.Scan(&eventID)
	if err == sql.ErrNoRows {
		return "", nil
	}
	return eventID, err
}

// GetAllStateEvents returns all current state events for a room.
func (db *DB) GetAllStateEvents(roomID string) ([]*models.Event, error) {
	rows, err := db.Query(
		`SELECT e.event_id, e.room_id, e.sender, e.event_type, e.state_key, e.content,
		 e.prev_events, e.origin_server_ts, e.depth, e.unsigned, e.redacts
		 FROM state_events s JOIN events e ON s.event_id = e.event_id
		 WHERE s.room_id = ?`,
		roomID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var events []*models.Event
	for rows.Next() {
		ev, err := scanEventFromRows(rows)
		if err != nil {
			return nil, err
		}
		events = append(events, ev)
	}
	return events, rows.Err()
}

// GetStateEventsByType returns current state events of a given type.
func (db *DB) GetStateEventsByType(roomID, eventType string) ([]*models.Event, error) {
	rows, err := db.Query(
		`SELECT e.event_id, e.room_id, e.sender, e.event_type, e.state_key, e.content,
		 e.prev_events, e.origin_server_ts, e.depth, e.unsigned, e.redacts
		 FROM state_events s JOIN events e ON s.event_id = e.event_id
		 WHERE s.room_id = ? AND s.event_type = ?`,
		roomID, eventType,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var events []*models.Event
	for rows.Next() {
		ev, err := scanEventFromRows(rows)
		if err != nil {
			return nil, err
		}
		events = append(events, ev)
	}
	return events, rows.Err()
}

// GetStateContent returns the content of a state event.
func (db *DB) GetStateContent(roomID, eventType, stateKey string) ([]byte, error) {
	row := db.QueryRow(
		`SELECT e.content FROM state_events s JOIN events e ON s.event_id = e.event_id
		 WHERE s.room_id = ? AND s.event_type = ? AND s.state_key = ?`,
		roomID, eventType, stateKey,
	)
	var content []byte
	err := row.Scan(&content)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return content, err
}

// GetRoomName returns the room's m.room.name content body.name if set.
func (db *DB) GetRoomName(roomID string) (string, error) {
	content, err := db.GetStateContent(roomID, "m.room.name", "")
	if err != nil || content == nil {
		return "", err
	}
	var parsed struct {
		Name string `json:"name"`
	}
	_ = json.Unmarshal(content, &parsed)
	return parsed.Name, nil
}

// RebuildStateFromTimeline recomputes current state from timeline events.
// This is a slow fallback and not used in the hot path.
func (db *DB) RebuildStateFromTimeline(roomID string) error {
	rows, err := db.Query(
		`SELECT e.event_id, e.event_type, e.state_key FROM timeline t
		 JOIN events e ON t.event_id = e.event_id
		 WHERE t.room_id = ? AND e.state_key IS NOT NULL
		 ORDER BY t.stream_ordering ASC`,
		roomID,
	)
	if err != nil {
		return err
	}
	defer rows.Close()

	return db.InTransaction(func(tx *sql.Tx) error {
		_, _ = tx.Exec(`DELETE FROM state_events WHERE room_id = ?`, roomID)
		for rows.Next() {
			var eventID, eventType, stateKey string
			if err := rows.Scan(&eventID, &eventType, &stateKey); err != nil {
				return err
			}
			if _, err := tx.Exec(
				`INSERT INTO state_events (room_id, event_type, state_key, event_id) VALUES (?, ?, ?, ?)
				 ON CONFLICT(room_id, event_type, state_key) DO UPDATE SET event_id=excluded.event_id`,
				roomID, eventType, stateKey, eventID,
			); err != nil {
				return err
			}
		}
		return rows.Err()
	})
}
