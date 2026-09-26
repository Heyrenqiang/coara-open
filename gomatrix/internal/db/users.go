package db

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"gomatrix/internal/models"
)

// CreateUser creates a new user account.
func (db *DB) CreateUser(userID, passwordHash string, admin bool) error {
	_, err := db.Exec(
		`INSERT INTO users (user_id, password_hash, admin, created_at) VALUES (?, ?, ?, ?)`,
		userID, passwordHash, boolToInt(admin), time.Now().UnixMilli(),
	)
	if err != nil {
		return fmt.Errorf("create user: %w", err)
	}
	return nil
}

// SetPassword replaces a user's password hash (pairing 账号以配置为准重置).
func (db *DB) SetPassword(userID, passwordHash string) error {
	_, err := db.Exec(`UPDATE users SET password_hash = ? WHERE user_id = ?`, passwordHash, userID)
	if err != nil {
		return fmt.Errorf("set password: %w", err)
	}
	return nil
}

// GetUser retrieves a user by ID.
func (db *DB) GetUser(userID string) (*models.User, error) {
	return getUser(db.QueryRow, userID)
}

// GetUserTx retrieves a user by ID within a transaction.
func (db *DB) GetUserTx(tx *sql.Tx, userID string) (*models.User, error) {
	return getUser(tx.QueryRow, userID)
}

type queryRowFunc func(query string, args ...any) *sql.Row

func getUser(q queryRowFunc, userID string) (*models.User, error) {
	row := q(
		`SELECT user_id, password_hash, display_name, avatar_url, admin, created_at FROM users WHERE user_id = ?`,
		userID,
	)
	u := &models.User{}
	var ph string
	var created int64
	var dn, av sql.NullString
	err := row.Scan(&u.UserID, &ph, &dn, &av, &u.Admin, &created)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	u.PasswordHash = ph
	if dn.Valid {
		u.DisplayName = &dn.String
	}
	if av.Valid {
		u.AvatarURL = &av.String
	}
	u.CreatedAt = time.UnixMilli(created)
	return u, nil
}

// UserExists checks if a user exists.
func (db *DB) UserExists(userID string) (bool, error) {
	row := db.QueryRow(`SELECT 1 FROM users WHERE user_id = ?`, userID)
	var n int
	err := row.Scan(&n)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}

// UpdateProfile updates display name and/or avatar URL.
func (db *DB) UpdateProfile(userID, displayName, avatarURL string) error {
	_, err := db.Exec(
		`UPDATE users SET display_name = ?, avatar_url = ? WHERE user_id = ?`,
		displayName, avatarURL, userID,
	)
	return err
}

// CreateDevice creates a new device.
func (db *DB) CreateDevice(deviceID, userID, displayName string) error {
	_, err := db.Exec(
		`INSERT INTO devices (device_id, user_id, display_name) VALUES (?, ?, ?)`,
		deviceID, userID, displayName,
	)
	return err
}

// GetDevice retrieves a device.
func (db *DB) GetDevice(deviceID string) (*models.Device, error) {
	row := db.QueryRow(
		`SELECT device_id, user_id, display_name, last_seen_ip, last_seen_at FROM devices WHERE device_id = ?`,
		deviceID,
	)
	d := &models.Device{}
	var lastSeenIP sql.NullString
	var lastSeen sql.NullInt64
	// last_seen_ip/at stay NULL until the first /sync; must use Null* scanners
	// or re-login with a fixed device_id (e.g. COARA_CLI) fails as M_FORBIDDEN.
	err := row.Scan(&d.DeviceID, &d.UserID, &d.DisplayName, &lastSeenIP, &lastSeen)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if lastSeenIP.Valid {
		d.LastSeenIP = lastSeenIP.String
	}
	if lastSeen.Valid {
		t := time.UnixMilli(lastSeen.Int64)
		d.LastSeenAt = sql.NullTime{Time: t, Valid: true}
	}
	return d, nil
}

// GetDevicesForUser lists all devices of a user.
func (db *DB) GetDevicesForUser(userID string) ([]*models.Device, error) {
	rows, err := db.Query(
		`SELECT device_id, user_id, display_name, last_seen_ip, last_seen_at FROM devices WHERE user_id = ?`,
		userID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var devices []*models.Device
	for rows.Next() {
		d := &models.Device{}
		var lastSeenIP sql.NullString
		var lastSeen sql.NullInt64
		if err := rows.Scan(&d.DeviceID, &d.UserID, &d.DisplayName, &lastSeenIP, &lastSeen); err != nil {
			return nil, err
		}
		if lastSeenIP.Valid {
			d.LastSeenIP = lastSeenIP.String
		}
		if lastSeen.Valid {
			t := time.UnixMilli(lastSeen.Int64)
			d.LastSeenAt = sql.NullTime{Time: t, Valid: true}
		}
		devices = append(devices, d)
	}
	return devices, rows.Err()
}

// UpdateDevice updates device metadata.
func (db *DB) UpdateDevice(deviceID, displayName string) error {
	_, err := db.Exec(`UPDATE devices SET display_name = ? WHERE device_id = ?`, displayName, deviceID)
	return err
}

// DeleteDevice deletes a device and its tokens.
func (db *DB) DeleteDevice(deviceID string) error {
	_, err := db.Exec(`DELETE FROM devices WHERE device_id = ?`, deviceID)
	return err
}

// UpdateDeviceLastSeen records the last IP and time for a device.
func (db *DB) UpdateDeviceLastSeen(deviceID, ip string, at int64) error {
	_, err := db.Exec(
		`UPDATE devices SET last_seen_ip = ?, last_seen_at = ? WHERE device_id = ?`,
		ip, at, deviceID,
	)
	return err
}

// CreateAccessToken stores a new access token.
func (db *DB) CreateAccessToken(token, userID, deviceID string) error {
	_, err := db.Exec(
		`INSERT INTO access_tokens (token, user_id, device_id, created_at) VALUES (?, ?, ?, ?)`,
		token, userID, deviceID, time.Now().UnixMilli(),
	)
	return err
}

// GetTokenUser returns the user/device associated with a token.
func (db *DB) GetTokenUser(token string) (userID, deviceID string, err error) {
	row := db.QueryRow(`SELECT user_id, device_id FROM access_tokens WHERE token = ?`, token)
	err = row.Scan(&userID, &deviceID)
	if err == sql.ErrNoRows {
		return "", "", nil
	}
	return userID, deviceID, err
}

// DeleteAccessToken deletes a token.
func (db *DB) DeleteAccessToken(token string) error {
	_, err := db.Exec(`DELETE FROM access_tokens WHERE token = ?`, token)
	return err
}

// DeleteAccessTokensForDevice deletes all tokens for a device.
func (db *DB) DeleteAccessTokensForDevice(deviceID string) error {
	_, err := db.Exec(`DELETE FROM access_tokens WHERE device_id = ?`, deviceID)
	return err
}

// UserRecentlyActive reports whether any device for the user synced within maxIdleMs.
func (db *DB) UserRecentlyActive(userID string, maxIdleMs int64) (bool, error) {
	var lastSeen sql.NullInt64
	err := db.QueryRow(`SELECT MAX(last_seen_at) FROM devices WHERE user_id = ?`, userID).Scan(&lastSeen)
	if err != nil {
		return false, err
	}
	if !lastSeen.Valid {
		return false, nil
	}
	return time.Now().UnixMilli()-lastSeen.Int64 <= maxIdleMs, nil
}

// RecentlyActiveUserIDs returns distinct user_ids with any device last_seen within maxIdleMs.
func (db *DB) RecentlyActiveUserIDs(maxIdleMs int64) ([]string, error) {
	cutoff := time.Now().UnixMilli() - maxIdleMs
	rows, err := db.Query(
		`SELECT DISTINCT user_id FROM devices
		 WHERE last_seen_at IS NOT NULL AND last_seen_at >= ?`,
		cutoff,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var uid string
		if err := rows.Scan(&uid); err != nil {
			return nil, err
		}
		out = append(out, uid)
	}
	return out, rows.Err()
}

// GetAccountData returns account data for a user, optionally scoped to a room.
func (db *DB) GetAccountData(userID string, roomID *string, dataType string) (json.RawMessage, error) {
	var r sql.NullString
	if roomID != nil {
		r.Valid = true
		r.String = *roomID
	}
	row := db.QueryRow(
		`SELECT content FROM account_data WHERE user_id = ? AND room_id IS ? AND type = ?`,
		userID, r, dataType,
	)
	var content string
	err := row.Scan(&content)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return json.RawMessage(content), nil
}

// GetAllAccountData returns all account data for a user.
func (db *DB) GetAllAccountData(userID string) ([]models.AccountData, error) {
	rows, err := db.Query(
		`SELECT room_id, type, content FROM account_data WHERE user_id = ?`,
		userID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []models.AccountData
	for rows.Next() {
		var roomID sql.NullString
		var d models.AccountData
		d.UserID = userID
		var content string
		if err := rows.Scan(&roomID, &d.Type, &content); err != nil {
			return nil, err
		}
		if roomID.Valid {
			d.RoomID = &roomID.String
		}
		d.Content = json.RawMessage(content)
		out = append(out, d)
	}
	return out, rows.Err()
}

// SetAccountData stores account data.
func (db *DB) SetAccountData(userID string, roomID *string, dataType string, content json.RawMessage) error {
	var r sql.NullString
	if roomID != nil {
		r.Valid = true
		r.String = *roomID
	}
	_, err := db.Exec(
		`INSERT INTO account_data (user_id, room_id, type, content) VALUES (?, ?, ?, ?)
		 ON CONFLICT(user_id, room_id, type) DO UPDATE SET content=excluded.content`,
		userID, r, dataType, string(content),
	)
	return err
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
