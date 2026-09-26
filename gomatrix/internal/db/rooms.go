package db

import (
	"database/sql"
	"fmt"
	"time"

	"gomatrix/internal/models"
)

// CreateRoom inserts a new room.
func (db *DB) CreateRoom(room *models.Room) error {
	_, err := db.Exec(
		`INSERT INTO rooms (room_id, version, creator, name, topic, canonical_alias, encrypted, join_rules, created_at)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		room.RoomID, room.Version, room.Creator, room.Name, room.Topic, room.CanonicalAlias,
		boolToInt(room.Encrypted), room.JoinRules, room.CreatedAt.UnixMilli(),
	)
	if err != nil {
		return fmt.Errorf("create room: %w", err)
	}
	return nil
}

// GetRoom retrieves a room.
func (db *DB) GetRoom(roomID string) (*models.Room, error) {
	row := db.QueryRow(
		`SELECT room_id, version, creator, name, topic, canonical_alias, encrypted, join_rules, created_at
		 FROM rooms WHERE room_id = ?`, roomID,
	)
	r := &models.Room{}
	var enc int
	var created int64
	var name, topic, canAlias sql.NullString
	err := row.Scan(&r.RoomID, &r.Version, &r.Creator, &name, &topic, &canAlias, &enc, &r.JoinRules, &created)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if name.Valid {
		r.Name = &name.String
	}
	if topic.Valid {
		r.Topic = &topic.String
	}
	if canAlias.Valid {
		r.CanonicalAlias = &canAlias.String
	}
	r.Encrypted = enc != 0
	r.CreatedAt = time.UnixMilli(created)
	return r, nil
}

// GetRoomByAlias looks up a room by alias.
func (db *DB) GetRoomByAlias(alias string) (roomID string, err error) {
	row := db.QueryRow(`SELECT room_id FROM room_aliases WHERE alias = ?`, alias)
	err = row.Scan(&roomID)
	if err == sql.ErrNoRows {
		return "", nil
	}
	return roomID, err
}

// GetAlias returns the room ID and creator for an alias, or empty strings if unknown.
func (db *DB) GetAlias(alias string) (roomID, creator string, err error) {
	row := db.QueryRow(`SELECT room_id, creator FROM room_aliases WHERE alias = ?`, alias)
	err = row.Scan(&roomID, &creator)
	if err == sql.ErrNoRows {
		return "", "", nil
	}
	return roomID, creator, err
}

// CreateAlias creates a room alias.
func (db *DB) CreateAlias(alias, roomID, creator string) error {
	_, err := db.Exec(
		`INSERT INTO room_aliases (alias, room_id, creator) VALUES (?, ?, ?)`,
		alias, roomID, creator,
	)
	return err
}

// DeleteAlias removes a room alias.
func (db *DB) DeleteAlias(alias string) error {
	_, err := db.Exec(`DELETE FROM room_aliases WHERE alias = ?`, alias)
	return err
}

// SetRoomCanonicalAlias updates the room's canonical alias.
func (db *DB) SetRoomCanonicalAlias(roomID, alias string) error {
	_, err := db.Exec(`UPDATE rooms SET canonical_alias = ? WHERE room_id = ?`, alias, roomID)
	return err
}

// SetMembership sets the membership of a user in a room.
func (db *DB) SetMembership(roomID, userID, membership, displayName, avatarURL string) error {
	return setMembership(db.Exec, roomID, userID, membership, displayName, avatarURL)
}

// SetMembershipTx sets membership within a transaction.
func (db *DB) SetMembershipTx(tx *sql.Tx, roomID, userID, membership, displayName, avatarURL string) error {
	return setMembership(tx.Exec, roomID, userID, membership, displayName, avatarURL)
}

type execFunc func(query string, args ...any) (sql.Result, error)

func setMembership(exec execFunc, roomID, userID, membership, displayName, avatarURL string) error {
	_, err := exec(
		`INSERT INTO room_members (room_id, user_id, membership, display_name, avatar_url)
		 VALUES (?, ?, ?, ?, ?)
		 ON CONFLICT(room_id, user_id) DO UPDATE SET membership=excluded.membership,
		   display_name=excluded.display_name, avatar_url=excluded.avatar_url`,
		roomID, userID, membership, displayName, avatarURL,
	)
	return err
}

// GetMembership returns the current membership of a user in a room.
func (db *DB) GetMembership(roomID, userID string) (string, error) {
	row := db.QueryRow(
		`SELECT membership FROM room_members WHERE room_id = ? AND user_id = ?`,
		roomID, userID,
	)
	var m string
	err := row.Scan(&m)
	if err == sql.ErrNoRows {
		return "", nil
	}
	return m, err
}

// GetMembershipTx returns membership using an open transaction (sees uncommitted writes).
func (db *DB) GetMembershipTx(tx *sql.Tx, roomID, userID string) (string, error) {
	row := tx.QueryRow(
		`SELECT membership FROM room_members WHERE room_id = ? AND user_id = ?`,
		roomID, userID,
	)
	var m string
	err := row.Scan(&m)
	if err == sql.ErrNoRows {
		return "", nil
	}
	return m, err
}

// GetRoomMembers returns all members with the given membership, or all if empty.
func (db *DB) GetRoomMembers(roomID string, membership ...string) ([]*models.RoomMember, error) {
	var rows *sql.Rows
	var err error
	if len(membership) == 0 {
		rows, err = db.Query(
			`SELECT room_id, user_id, membership, display_name, avatar_url FROM room_members WHERE room_id = ?`,
			roomID,
		)
	} else {
		args := []any{roomID}
		for _, m := range membership {
			args = append(args, m)
		}
		rows, err = db.Query(
			`SELECT room_id, user_id, membership, display_name, avatar_url FROM room_members
			 WHERE room_id = ? AND membership IN (`+placeholders(len(membership))+`)`,
			args...,
		)
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var members []*models.RoomMember
	for rows.Next() {
		m := &models.RoomMember{}
		if err := rows.Scan(&m.RoomID, &m.UserID, &m.Membership, &m.DisplayName, &m.AvatarURL); err != nil {
			return nil, err
		}
		members = append(members, m)
	}
	return members, rows.Err()
}

// CountRoomMembers counts members with the given membership.
func (db *DB) CountRoomMembers(roomID string, membership ...string) (int, error) {
	var row *sql.Row
	if len(membership) == 0 {
		row = db.QueryRow(
			`SELECT COUNT(*) FROM room_members WHERE room_id = ?`, roomID,
		)
	} else {
		args := []any{roomID}
		for _, m := range membership {
			args = append(args, m)
		}
		row = db.QueryRow(
			`SELECT COUNT(*) FROM room_members WHERE room_id = ? AND membership IN (`+placeholders(len(membership))+`)`,
			args...,
		)
	}
	var n int
	err := row.Scan(&n)
	return n, err
}

// GetJoinedRoomsForUser returns all room IDs the user has joined.
func (db *DB) GetJoinedRoomsForUser(userID string) ([]string, error) {
	rows, err := db.Query(
		`SELECT room_id FROM room_members WHERE user_id = ? AND membership = ?`,
		userID, models.MembershipJoin,
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

// GetInvitedRoomsForUser returns rooms where the user is invited.
func (db *DB) GetInvitedRoomsForUser(userID string) ([]string, error) {
	rows, err := db.Query(
		`SELECT room_id FROM room_members WHERE user_id = ? AND membership = ?`,
		userID, models.MembershipInvite,
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

// GetLeftRoomsForUser returns rooms where the user has left.
func (db *DB) GetLeftRoomsForUser(userID string) ([]string, error) {
	rows, err := db.Query(
		`SELECT room_id FROM room_members WHERE user_id = ? AND membership = ?`,
		userID, models.MembershipLeave,
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

// GetAllJoinedMembers returns all joined members across rooms.
func (db *DB) GetAllJoinedMembers() (map[string][]string, error) {
	rows, err := db.Query(
		`SELECT room_id, user_id FROM room_members WHERE membership = ?`,
		models.MembershipJoin,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := make(map[string][]string)
	for rows.Next() {
		var roomID, userID string
		if err := rows.Scan(&roomID, &userID); err != nil {
			return nil, err
		}
		out[roomID] = append(out[roomID], userID)
	}
	return out, rows.Err()
}

func placeholders(n int) string {
	if n <= 0 {
		return ""
	}
	s := "?"
	for i := 1; i < n; i++ {
		s += ",?"
	}
	return s
}
