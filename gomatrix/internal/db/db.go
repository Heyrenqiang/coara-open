package db

import (
	_ "embed"
	"fmt"
	"strings"
	"sync"
	"time"

	"database/sql"
	"gomatrix/internal/config"
	_ "modernc.org/sqlite"
)

//go:embed schema.sql
var schemaSQL string

// DB wraps a sql.DB with homeserver-specific helpers.
type DB struct {
	*sql.DB
	serverName string
	writeMu    sync.Mutex
}

// Open opens the SQLite database and runs migrations.
func Open(cfg *config.Config) (*DB, error) {
	dsn := fmt.Sprintf("%s?_pragma=foreign_keys(1)&_pragma=journal_mode(WAL)&_pragma=synchronous(NORMAL)&_pragma=busy_timeout(10000)", cfg.DatabasePath)
	sqldb, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	if err := sqldb.Ping(); err != nil {
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}

	db := &DB{DB: sqldb, serverName: cfg.ServerName}
	if err := db.migrate(); err != nil {
		_ = sqldb.Close()
		return nil, fmt.Errorf("migrate: %w", err)
	}

	return db, nil
}

// schemaVersion is the current schema revision. The embedded baseline schema
// is version 1; every later schema change appends one incremental migration.
const schemaVersion = 1

// migration is one idempotent schema step. Migrations run in ascending
// version order and each sets PRAGMA user_version on success.
type migration struct {
	version int
	apply   func(*sql.DB) error
}

// migrations is the ordered migration chain. Version 1 is the baseline
// embedded schema (CREATE TABLE IF NOT EXISTS, so re-applying is a no-op).
var migrations = []migration{
	{version: 1, apply: func(d *sql.DB) error {
		if _, err := d.Exec(schemaSQL); err != nil {
			return fmt.Errorf("exec schema: %w", err)
		}
		return nil
	}},
}

// migrate brings the database up to schemaVersion. Any failure is returned
// to Open, which refuses to start the server on a database it cannot upgrade.
func (db *DB) migrate() error {
	var current int
	if err := db.QueryRow("PRAGMA user_version").Scan(&current); err != nil {
		return fmt.Errorf("read schema version: %w", err)
	}
	if current == 0 {
		legacy, err := db.hasUserTables()
		if err != nil {
			return err
		}
		if legacy {
			// Databases created before version tracking already carry the
			// full baseline schema (zero historical schema changes): mark
			// them as version 1 instead of re-running the baseline.
			current = 1
			if err := db.setSchemaVersion(1); err != nil {
				return err
			}
		}
	}
	if current > schemaVersion {
		return fmt.Errorf("database schema version %d is newer than supported version %d — upgrade gomatrix", current, schemaVersion)
	}
	for _, m := range migrations {
		if m.version <= current {
			continue
		}
		if err := m.apply(db.DB); err != nil {
			return fmt.Errorf("migrate to version %d: %w", m.version, err)
		}
		if err := db.setSchemaVersion(m.version); err != nil {
			return err
		}
	}
	return nil
}

// hasUserTables reports whether the database already holds application
// tables, i.e. it predates PRAGMA user_version tracking.
func (db *DB) hasUserTables() (bool, error) {
	var n int
	if err := db.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").Scan(&n); err != nil {
		return false, fmt.Errorf("inspect existing tables: %w", err)
	}
	return n > 0, nil
}

// setSchemaVersion records the applied schema revision. PRAGMA does not
// accept bound parameters, so the integer is inlined directly.
func (db *DB) setSchemaVersion(v int) error {
	if _, err := db.Exec(fmt.Sprintf("PRAGMA user_version = %d", v)); err != nil {
		return fmt.Errorf("set schema version %d: %w", v, err)
	}
	return nil
}

// ServerName returns the configured server name.
func (db *DB) ServerName() string {
	return db.serverName
}

// WaitWriteIdle blocks until in-flight DB writes finish (Conduit sync barrier).
func (db *DB) WaitWriteIdle() {
	db.writeMu.Lock()
	db.writeMu.Unlock()
}

// InTransaction runs fn inside a transaction and commits or rolls back.
// SQLite only allows one writer at a time, so this serializes write transactions.
func (db *DB) InTransaction(fn func(*sql.Tx) error) error {
	const maxAttempts = 6
	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		db.writeMu.Lock()
		lastErr = func() error {
			defer db.writeMu.Unlock()
			tx, err := db.Begin()
			if err != nil {
				return err
			}
			if err := fn(tx); err != nil {
				_ = tx.Rollback()
				return err
			}
			return tx.Commit()
		}()
		if lastErr == nil {
			return nil
		}
		if !isSQLiteBusy(lastErr) || attempt+1 >= maxAttempts {
			return lastErr
		}
		time.Sleep(time.Duration(40*(attempt+1)) * time.Millisecond)
	}
	return lastErr
}

func isSQLiteBusy(err error) bool {
	if err == nil {
		return false
	}
	msg := strings.ToLower(err.Error())
	return strings.Contains(msg, "database is locked") ||
		strings.Contains(msg, "sqlite_busy") ||
		strings.Contains(msg, "locked (5") ||
		strings.Contains(msg, "locked (517")
}

// IsBusy reports whether err is a transient SQLite writer lock.
func IsBusy(err error) bool {
	return isSQLiteBusy(err)
}
