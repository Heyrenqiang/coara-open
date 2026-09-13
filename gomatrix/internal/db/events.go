package db

import (
	"database/sql"
	"encoding/json"
	"fmt"

	"gomatrix/internal/models"
)

// SaveEvent stores an event and returns the stream ordering.
func (db *DB) SaveEvent(tx *sql.Tx, ev *models.Event) (int64, error) {
	content, _ := json.Marshal(ev.Content)
	prevEvents, _ := json.Marshal(ev.PrevEvents)
	unsigned, _ := json.Marshal(ev.Unsigned)

	var stateKey sql.NullString
	if ev.StateKey != nil {
		stateKey.Valid = true
		stateKey.String = *ev.StateKey
	}
	var redacts sql.NullString
	if ev.Redacts != nil {
		redacts.Valid = true
		redacts.String = *ev.Redacts
	}

	_, err := tx.Exec(
		`INSERT INTO events (event_id, room_id, sender, event_type, state_key, content, prev_events,
		 origin_server_ts, depth, unsigned, redacts)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		ev.EventID, ev.RoomID, ev.Sender, ev.Type, stateKey, string(content),
		string(prevEvents), ev.OriginServerTs, ev.Depth, string(unsigned), redacts,
	)
	if err != nil {
		return 0, fmt.Errorf("insert event: %w", err)
	}

	res, err := tx.Exec(`INSERT INTO timeline (room_id, event_id) VALUES (?, ?)`, ev.RoomID, ev.EventID)
	if err != nil {
		return 0, fmt.Errorf("insert timeline: %w", err)
	}
	stream, err := res.LastInsertId()
	if err != nil {
		return 0, err
	}
	return stream, nil
}

// DeleteVaultReplyEvents 删除早于 cutoffMs 的宝箱密码回信事件
// （timeline 行由外键 ON DELETE CASCADE 一并清除）。
func (db *DB) DeleteVaultReplyEvents(cutoffMs int64) (int64, error) {
	var n int64
	err := db.InTransaction(func(tx *sql.Tx) error {
		res, err := tx.Exec(
			`DELETE FROM events WHERE event_type = 'm.room.message'
			 AND content LIKE '%[COARA_VAULT_REPLY]%' AND origin_server_ts < ?`,
			cutoffMs,
		)
		if err != nil {
			return err
		}
		n, _ = res.RowsAffected()
		return nil
	})
	return n, err
}

// GetEvent retrieves an event by ID.
func (db *DB) GetEvent(eventID string) (*models.Event, error) {
	row := db.QueryRow(
		`SELECT event_id, room_id, sender, event_type, state_key, content, prev_events,
		 origin_server_ts, depth, unsigned, redacts FROM events WHERE event_id = ?`,
		eventID,
	)
	return scanEvent(row)
}

// TimelineEvent combines an event with its stream ordering.
type TimelineEvent struct {
	StreamOrdering int64
	Event          *models.Event
}

// GetTimelineForRoom returns timeline events with stream orderings in a range.
func (db *DB) GetTimelineForRoom(roomID string, from, to int64, limit int, backward bool) ([]TimelineEvent, error) {
	if limit <= 0 || limit > 1000 {
		limit = 1000
	}
	var rows *sql.Rows
	var err error
	if backward {
		rows, err = db.Query(
			`SELECT t.stream_ordering, e.event_id, e.room_id, e.sender, e.event_type, e.state_key, e.content,
			 e.prev_events, e.origin_server_ts, e.depth, e.unsigned, e.redacts
			 FROM timeline t JOIN events e ON t.event_id = e.event_id
			 WHERE t.room_id = ? AND t.stream_ordering < ? AND t.stream_ordering >= ?
			 ORDER BY t.stream_ordering DESC LIMIT ?`,
			roomID, from, to, limit,
		)
	} else {
		rows, err = db.Query(
			`SELECT t.stream_ordering, e.event_id, e.room_id, e.sender, e.event_type, e.state_key, e.content,
			 e.prev_events, e.origin_server_ts, e.depth, e.unsigned, e.redacts
			 FROM timeline t JOIN events e ON t.event_id = e.event_id
			 WHERE t.room_id = ? AND t.stream_ordering > ? AND t.stream_ordering <= ?
			 ORDER BY t.stream_ordering ASC LIMIT ?`,
			roomID, from, to, limit,
		)
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []TimelineEvent
	for rows.Next() {
		var te TimelineEvent
		ev, err := scanTimelineEventFromRows(rows, &te.StreamOrdering)
		if err != nil {
			return nil, err
		}
		te.Event = ev
		out = append(out, te)
	}
	return out, rows.Err()
}

// GetMaxStreamOrdering returns the highest timeline position globally.
func (db *DB) GetMaxStreamOrdering() (int64, error) {
	row := db.QueryRow(`SELECT COALESCE(MAX(stream_ordering), 0) FROM timeline`)
	var n int64
	err := row.Scan(&n)
	return n, err
}

// HasStreamUpdatesForUser reports timeline activity after since in rooms the user joined/invited.
func (db *DB) HasStreamUpdatesForUser(userID string, since int64) (bool, error) {
	row := db.QueryRow(
		`SELECT 1 FROM timeline t
		 INNER JOIN room_members m ON m.room_id = t.room_id AND m.user_id = ?
		 WHERE t.stream_ordering > ? AND m.membership IN (?, ?) LIMIT 1`,
		userID, since, models.MembershipJoin, models.MembershipInvite,
	)
	var one int
	err := row.Scan(&one)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}

// HasJoinStreamUpdatesForUser reports timeline activity after since in rooms the user has joined.
// Used for next_batch advancement — invite-only rooms deliver timeline via rooms.join separately.
func (db *DB) HasJoinStreamUpdatesForUser(userID string, since int64) (bool, error) {
	row := db.QueryRow(
		`SELECT 1 FROM timeline t
		 INNER JOIN room_members m ON m.room_id = t.room_id AND m.user_id = ?
		 WHERE t.stream_ordering > ? AND m.membership = ? LIMIT 1`,
		userID, since, models.MembershipJoin,
	)
	var one int
	err := row.Scan(&one)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}

// HasRoomStreamUpdatesSince reports timeline activity in one room after since.
func (db *DB) HasRoomStreamUpdatesSince(roomID string, since int64) (bool, error) {
	row := db.QueryRow(
		`SELECT 1 FROM timeline WHERE room_id = ? AND stream_ordering > ? LIMIT 1`,
		roomID, since,
	)
	var one int
	err := row.Scan(&one)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}

// HasStreamUpdatesSince reports whether any timeline event exists after since.
func (db *DB) HasStreamUpdatesSince(since int64) (bool, error) {
	row := db.QueryRow(`SELECT 1 FROM timeline WHERE stream_ordering > ? LIMIT 1`, since)
	var one int
	err := row.Scan(&one)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}

// GetDirtyJoinedRoomIDs returns joined/invited rooms with timeline activity after since.
func (db *DB) GetDirtyJoinedRoomIDs(userID string, since int64) ([]string, error) {
	rows, err := db.Query(
		`SELECT DISTINCT t.room_id FROM timeline t
		 INNER JOIN room_members m ON m.room_id = t.room_id AND m.user_id = ?
		 WHERE t.stream_ordering > ? AND m.membership IN (?, ?)`,
		userID, since, models.MembershipJoin, models.MembershipInvite,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var rooms []string
	for rows.Next() {
		var roomID string
		if err := rows.Scan(&roomID); err != nil {
			return nil, err
		}
		rooms = append(rooms, roomID)
	}
	return rooms, rows.Err()
}

// GetSyncNotifyUserIDsForRoom returns users who should wake on room activity (join + invite).
func (db *DB) GetSyncNotifyUserIDsForRoom(roomID string) ([]string, error) {
	rows, err := db.Query(
		`SELECT user_id FROM room_members WHERE room_id = ? AND membership IN (?, ?)`,
		roomID, models.MembershipJoin, models.MembershipInvite,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var users []string
	for rows.Next() {
		var userID string
		if err := rows.Scan(&userID); err != nil {
			return nil, err
		}
		users = append(users, userID)
	}
	return users, rows.Err()
}

// GetJoinedUserIDsForRoom returns user IDs with join membership in a room.
func (db *DB) GetJoinedUserIDsForRoom(roomID string) ([]string, error) {
	rows, err := db.Query(
		`SELECT user_id FROM room_members WHERE room_id = ? AND membership = ?`,
		roomID, models.MembershipJoin,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var users []string
	for rows.Next() {
		var userID string
		if err := rows.Scan(&userID); err != nil {
			return nil, err
		}
		users = append(users, userID)
	}
	return users, rows.Err()
}

// LookupSentTransaction returns a prior event_id for a send txn replay.
func (db *DB) LookupSentTransaction(userID, roomID, txnID string) (string, error) {
	row := db.QueryRow(
		`SELECT event_id FROM sent_transactions WHERE user_id = ? AND room_id = ? AND txn_id = ?`,
		userID, roomID, txnID,
	)
	var eventID string
	err := row.Scan(&eventID)
	if err == sql.ErrNoRows {
		return "", nil
	}
	return eventID, err
}

// LookupSentTransactionTx is the transactional form of LookupSentTransaction.
func (db *DB) LookupSentTransactionTx(tx *sql.Tx, userID, roomID, txnID string) (string, error) {
	row := tx.QueryRow(
		`SELECT event_id FROM sent_transactions WHERE user_id = ? AND room_id = ? AND txn_id = ?`,
		userID, roomID, txnID,
	)
	var eventID string
	err := row.Scan(&eventID)
	if err == sql.ErrNoRows {
		return "", nil
	}
	return eventID, err
}

// SaveSentTransaction records a successful send txn for deduplication.
func (db *DB) SaveSentTransaction(tx *sql.Tx, userID, roomID, txnID, eventID string, createdAt int64) error {
	_, err := tx.Exec(
		`INSERT INTO sent_transactions (user_id, room_id, txn_id, event_id, created_at)
		 VALUES (?, ?, ?, ?, ?)
		 ON CONFLICT(user_id, room_id, txn_id) DO NOTHING`,
		userID, roomID, txnID, eventID, createdAt,
	)
	return err
}

// GetRoomMaxStreamOrdering returns the highest timeline position in a room.
func (db *DB) GetRoomMaxStreamOrdering(roomID string) (int64, error) {
	row := db.QueryRow(`SELECT COALESCE(MAX(stream_ordering), 0) FROM timeline WHERE room_id = ?`, roomID)
	var n int64
	err := row.Scan(&n)
	return n, err
}

func scanEvent(row *sql.Row) (*models.Event, error) {
	var ev models.Event
	var content, prevEvents, unsigned string
	var stateKey, redacts sql.NullString
	err := row.Scan(&ev.EventID, &ev.RoomID, &ev.Sender, &ev.Type, &stateKey, &content,
		&prevEvents, &ev.OriginServerTs, &ev.Depth, &unsigned, &redacts)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	_ = json.Unmarshal([]byte(content), &ev.Content)
	_ = json.Unmarshal([]byte(prevEvents), &ev.PrevEvents)
	_ = json.Unmarshal([]byte(unsigned), &ev.Unsigned)
	if stateKey.Valid {
		ev.StateKey = &stateKey.String
	}
	if redacts.Valid {
		ev.Redacts = &redacts.String
	}
	normalizeEventContent(&ev)
	return &ev, nil
}

func normalizeEventContent(ev *models.Event) {
	if ev == nil {
		return
	}
	if len(ev.Content) == 0 || string(ev.Content) == "null" {
		ev.Content = json.RawMessage("{}")
	}
	if len(ev.Unsigned) == 0 || string(ev.Unsigned) == "null" {
		ev.Unsigned = json.RawMessage("{}")
	}
}

func scanTimelineEventFromRows(rows *sql.Rows, stream *int64) (*models.Event, error) {
	var ev models.Event
	var content, prevEvents, unsigned string
	var stateKey, redacts sql.NullString
	err := rows.Scan(stream, &ev.EventID, &ev.RoomID, &ev.Sender, &ev.Type, &stateKey, &content,
		&prevEvents, &ev.OriginServerTs, &ev.Depth, &unsigned, &redacts)
	if err != nil {
		return nil, err
	}
	_ = json.Unmarshal([]byte(content), &ev.Content)
	_ = json.Unmarshal([]byte(prevEvents), &ev.PrevEvents)
	_ = json.Unmarshal([]byte(unsigned), &ev.Unsigned)
	if stateKey.Valid {
		ev.StateKey = &stateKey.String
	}
	if redacts.Valid {
		ev.Redacts = &redacts.String
	}
	normalizeEventContent(&ev)
	return &ev, nil
}

func scanEventFromRows(rows *sql.Rows) (*models.Event, error) {
	var ev models.Event
	var content, prevEvents, unsigned string
	var stateKey, redacts sql.NullString
	err := rows.Scan(&ev.EventID, &ev.RoomID, &ev.Sender, &ev.Type, &stateKey, &content,
		&prevEvents, &ev.OriginServerTs, &ev.Depth, &unsigned, &redacts)
	if err != nil {
		return nil, err
	}
	_ = json.Unmarshal([]byte(content), &ev.Content)
	_ = json.Unmarshal([]byte(prevEvents), &ev.PrevEvents)
	_ = json.Unmarshal([]byte(unsigned), &ev.Unsigned)
	if stateKey.Valid {
		ev.StateKey = &stateKey.String
	}
	if redacts.Valid {
		ev.Redacts = &redacts.String
	}
	normalizeEventContent(&ev)
	return &ev, nil
}
