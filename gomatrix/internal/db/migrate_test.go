package db

import (
	"database/sql"
	"path/filepath"
	"strings"
	"testing"

	"gomatrix/internal/config"
	_ "modernc.org/sqlite"
)

func userVersion(t *testing.T, db *DB) int {
	t.Helper()
	var v int
	if err := db.QueryRow("PRAGMA user_version").Scan(&v); err != nil {
		t.Fatalf("read user_version: %v", err)
	}
	return v
}

func TestMigrateFreshDatabaseSetsBaselineVersion(t *testing.T) {
	path := filepath.Join(t.TempDir(), "test.db")
	cfg := &config.Config{ServerName: "test.local", DatabasePath: path}

	db, err := Open(cfg)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	if v := userVersion(t, db); v != 1 {
		t.Fatalf("user_version = %d, want 1", v)
	}
	db.Close()

	// Reopening is idempotent: version stays 1 and migration is a no-op.
	db, err = Open(cfg)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	if v := userVersion(t, db); v != 1 {
		t.Fatalf("after reopen user_version = %d, want 1", v)
	}
}

// TestMigrateLegacyDatabaseMarkedAsBaseline simulates a database created
// before version tracking: full schema present, user_version = 0. It must be
// adopted as version 1 without re-running the baseline migration.
func TestMigrateLegacyDatabaseMarkedAsBaseline(t *testing.T) {
	path := filepath.Join(t.TempDir(), "legacy.db")

	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("open raw: %v", err)
	}
	if _, err := raw.Exec(schemaSQL); err != nil {
		t.Fatalf("seed legacy schema: %v", err)
	}
	var v int
	if err := raw.QueryRow("PRAGMA user_version").Scan(&v); err != nil {
		t.Fatalf("read legacy user_version: %v", err)
	}
	if v != 0 {
		t.Fatalf("legacy user_version = %d, want 0", v)
	}
	raw.Close()

	cfg := &config.Config{ServerName: "test.local", DatabasePath: path}
	db, err := Open(cfg)
	if err != nil {
		t.Fatalf("open legacy db: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	if v := userVersion(t, db); v != 1 {
		t.Fatalf("legacy db user_version = %d, want 1 (adopted as baseline)", v)
	}
}

// TestMigrateNewerVersionRefusesStartup guards against running an old binary
// on a database migrated by a newer one: Open must fail loudly.
func TestMigrateNewerVersionRefusesStartup(t *testing.T) {
	path := filepath.Join(t.TempDir(), "future.db")
	cfg := &config.Config{ServerName: "test.local", DatabasePath: path}

	db, err := Open(cfg)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	if _, err := db.Exec("PRAGMA user_version = 99"); err != nil {
		t.Fatalf("bump version: %v", err)
	}
	db.Close()

	db, err = Open(cfg)
	if err == nil {
		db.Close()
		t.Fatal("open with newer schema version succeeded, want refusal")
	}
	if !strings.Contains(err.Error(), "newer than supported") {
		t.Fatalf("error = %v, want 'newer than supported'", err)
	}
}
