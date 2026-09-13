package api

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"gomatrix/internal/config"
	"gomatrix/internal/db"
	"gomatrix/internal/service"
)

// TestKeyServerEndpointRequiresFederation verifies /_matrix/key/v2/server is
// only mounted when federation is enabled (same condition as /federation/send).
func TestKeyServerEndpointRequiresFederation(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		ServerName:         "test.local",
		DatabasePath:       filepath.Join(dir, "test.db"),
		Address:            "127.0.0.1",
		Port:               0,
		MaxRequestSize:     1024 * 1024,
		DefaultRoomVersion: "10",
		MediaPath:          filepath.Join(dir, "media"),
		LogLevel:           "error",
		AllowFederation:    false,
	}
	database, err := db.Open(cfg)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() { database.Close() })
	service.SetGlobal(service.Build(cfg, database))

	cfg.AllowFederation = false
	rr := httptest.NewRecorder()
	NewRouter().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/_matrix/key/v2/server", nil))
	if rr.Code != http.StatusNotFound {
		t.Fatalf("federation disabled: status = %d, want 404 (body: %s)", rr.Code, rr.Body.String())
	}

	cfg.AllowFederation = true
	rr = httptest.NewRecorder()
	NewRouter().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/_matrix/key/v2/server", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("federation enabled: status = %d, want 200 (body: %s)", rr.Code, rr.Body.String())
	}
}

// TestCoaraAPIProxyRequiresAuth verifies /coara-api/* sits behind Matrix auth:
// without an access token the proxy must never reach the coara web server.
func TestCoaraAPIProxyRequiresAuth(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		ServerName:         "test.local",
		DatabasePath:       filepath.Join(dir, "test.db"),
		Address:            "127.0.0.1",
		Port:               0,
		MaxRequestSize:     1024 * 1024,
		DefaultRoomVersion: "10",
		MediaPath:          filepath.Join(dir, "media"),
		LogLevel:           "error",
		CoaraAPI:           config.CoaraAPIConfig{Enabled: true, UpstreamPort: 1, TokenFile: filepath.Join(dir, "nope")},
	}
	database, err := db.Open(cfg)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() { database.Close() })
	service.SetGlobal(service.Build(cfg, database))

	rr := httptest.NewRecorder()
	NewRouter().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/coara-api/usage/dashboard", nil))
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("no token: status = %d, want 401 (body: %s)", rr.Code, rr.Body.String())
	}

	cfg.CoaraAPI.Enabled = false
	rr = httptest.NewRecorder()
	NewRouter().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/coara-api/usage/dashboard", nil))
	if rr.Code == http.StatusUnauthorized {
		t.Fatalf("proxy disabled: status = %d, want non-401 (not mounted)", rr.Code)
	}
}
