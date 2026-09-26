package config

import (
	"os"
	"path/filepath"
	"testing"
)

// writeConfig creates a minimal gomatrix.toml in dir with relative data paths.
func writeConfig(t *testing.T, dir string) string {
	t.Helper()
	path := filepath.Join(dir, "gomatrix.toml")
	content := "server_name = \"test.local\"\ndatabase_path = \"./gomatrix.db\"\nmedia_path = \"./media\"\n"
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}
	return path
}

// clearEnv blanks GOMAX_* overlays so the host environment cannot leak into tests.
func clearEnv(t *testing.T) {
	t.Helper()
	t.Setenv("GOMAX_DATABASE_PATH", "")
	t.Setenv("GOMAX_MEDIA_PATH", "")
}

func TestLoadAnchorsRelativePathsToConfigDir(t *testing.T) {
	clearEnv(t)
	cfgDir := t.TempDir()
	cfgPath := writeConfig(t, cfgDir)

	// Existing database next to the config file must be found even when the
	// process runs from a different working directory.
	dbFile := filepath.Join(cfgDir, "gomatrix.db")
	if err := os.WriteFile(dbFile, []byte("db"), 0o644); err != nil {
		t.Fatalf("write db: %v", err)
	}
	mediaDir := filepath.Join(cfgDir, "media")
	if err := os.MkdirAll(mediaDir, 0o755); err != nil {
		t.Fatalf("mkdir media: %v", err)
	}

	workDir := t.TempDir()
	t.Chdir(workDir)

	cfg, err := Load(cfgPath)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.DatabasePath != dbFile {
		t.Fatalf("DatabasePath = %q, want anchored %q", cfg.DatabasePath, dbFile)
	}
	if cfg.MediaPath != mediaDir {
		t.Fatalf("MediaPath = %q, want anchored %q", cfg.MediaPath, mediaDir)
	}
}

func TestLoadFallsBackToCWDRelativePaths(t *testing.T) {
	clearEnv(t)
	cfgDir := t.TempDir()
	cfgPath := writeConfig(t, cfgDir)

	// Legacy pre-data-dir layout: data files exist relative to the working
	// directory but not next to the config file — keep using them.
	workDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(workDir, "gomatrix.db"), []byte("db"), 0o644); err != nil {
		t.Fatalf("write cwd db: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(workDir, "media"), 0o755); err != nil {
		t.Fatalf("mkdir cwd media: %v", err)
	}
	t.Chdir(workDir)

	cfg, err := Load(cfgPath)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.DatabasePath != "./gomatrix.db" {
		t.Fatalf("DatabasePath = %q, want CWD-relative fallback %q", cfg.DatabasePath, "./gomatrix.db")
	}
	if cfg.MediaPath != "./media" {
		t.Fatalf("MediaPath = %q, want CWD-relative fallback %q", cfg.MediaPath, "./media")
	}
}

func TestLoadAnchorsFreshPathsWhenNeitherExists(t *testing.T) {
	clearEnv(t)
	cfgDir := t.TempDir()
	cfgPath := writeConfig(t, cfgDir)

	workDir := t.TempDir()
	t.Chdir(workDir)

	cfg, err := Load(cfgPath)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	wantDB := filepath.Join(cfgDir, "gomatrix.db")
	if cfg.DatabasePath != wantDB {
		t.Fatalf("DatabasePath = %q, want anchored %q", cfg.DatabasePath, wantDB)
	}
	wantMedia := filepath.Join(cfgDir, "media")
	if cfg.MediaPath != wantMedia {
		t.Fatalf("MediaPath = %q, want anchored %q", cfg.MediaPath, wantMedia)
	}
}

func TestLoadMissingConfigKeepsDefaults(t *testing.T) {
	clearEnv(t)
	workDir := t.TempDir()
	t.Chdir(workDir)

	cfg, err := Load(filepath.Join(workDir, "does-not-exist.toml"))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.ServerName != "coara.local" {
		t.Fatalf("ServerName = %q, want default coara.local", cfg.ServerName)
	}
	// Missing config anchors to its own (working) directory: same file the
	// CWD-relative default would have used, now absolute.
	wantDB := filepath.Join(workDir, "gomatrix.db")
	if cfg.DatabasePath != wantDB {
		t.Fatalf("DatabasePath = %q, want %q", cfg.DatabasePath, wantDB)
	}
}

func TestDefaultIsLocalAndRegistrationClosed(t *testing.T) {
	cfg := Default()
	if cfg.Address != "127.0.0.1" {
		t.Fatalf("Address = %q, want loopback-only default", cfg.Address)
	}
	if cfg.AllowRegistration {
		t.Fatal("AllowRegistration must default to false")
	}
}
