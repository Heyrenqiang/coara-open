package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"gomatrix/internal/config"
	"gomatrix/internal/db"
	"gomatrix/internal/service"
)

func TestDashboardGuardAllowsDirectLocalClients(t *testing.T) {
	ok := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	handler := dashboardLocalOnly(ok)

	for _, remoteAddr := range []string{
		"127.0.0.1:4321",
		"[::1]:4321",
		"192.168.1.20:4321",
		"10.0.0.5:4321",
		"172.16.3.4:4321",
		"169.254.1.1:4321",
	} {
		req := httptest.NewRequest(http.MethodGet, "/dashboard/", nil)
		req.RemoteAddr = remoteAddr
		rr := httptest.NewRecorder()
		handler.ServeHTTP(rr, req)
		if rr.Code != http.StatusOK {
			t.Fatalf("remote %s: status = %d, want 200", remoteAddr, rr.Code)
		}
	}
}

func TestDashboardGuardRejectsTunnelAndPublicSources(t *testing.T) {
	ok := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	handler := dashboardLocalOnly(ok)

	// cloudflared dials the server locally, so tunnel requests arrive from
	// 127.0.0.1 — the CF-* edge headers are the only reliable signal.
	for _, header := range []string{"CF-Connecting-IP", "CF-Ray", "CF-Visitor"} {
		req := httptest.NewRequest(http.MethodGet, "/dashboard/", nil)
		req.RemoteAddr = "127.0.0.1:4321"
		req.Header.Set(header, "x")
		rr := httptest.NewRecorder()
		handler.ServeHTTP(rr, req)
		if rr.Code != http.StatusForbidden {
			t.Fatalf("%s from loopback: status = %d, want 403", header, rr.Code)
		}
	}

	// Direct connections from non-private addresses are rejected too.
	req := httptest.NewRequest(http.MethodGet, "/dashboard/", nil)
	req.RemoteAddr = "203.0.113.9:443"
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusForbidden {
		t.Fatalf("public remote: status = %d, want 403", rr.Code)
	}
}

// TestDashboardRoutesBlockedThroughTunnel goes through the real router:
// dashboard routes must be reachable from loopback (waitForOwnServer and the
// desktop browser rely on this) and rejected for tunnel sources.
func TestDashboardRoutesBlockedThroughTunnel(t *testing.T) {
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
	}
	database, err := db.Open(cfg)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() { database.Close() })
	service.SetGlobal(service.Build(cfg, database))

	router := NewRouter()
	localReq := func(path string) *http.Request {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		req.RemoteAddr = "127.0.0.1:4321"
		return req
	}

	// Loopback self-check still works (waitForOwnServer contract).
	rr := httptest.NewRecorder()
	router.ServeHTTP(rr, localReq("/api/dashboard/status"))
	if rr.Code != http.StatusOK {
		t.Fatalf("local status: status = %d, want 200 (body: %s)", rr.Code, rr.Body.String())
	}
	var probe struct {
		LocalURLs []string `json:"local_urls"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &probe); err != nil {
		t.Fatalf("decode status: %v", err)
	}

	// 数据接口路由对隧道来源一律 403。
	for _, path := range []string{
		"/dashboard/qr.png",
		"/api/dashboard/status",
	} {
		req := localReq(path)
		req.Header.Set("CF-Connecting-IP", "198.51.100.23")
		req.Header.Set("CF-Ray", "abc123")
		rr := httptest.NewRecorder()
		router.ServeHTTP(rr, req)
		if rr.Code != http.StatusForbidden {
			t.Fatalf("tunnel %s: status = %d, want 403", path, rr.Code)
		}
	}

	// 独立 dashboard 页面已退役：根路径不再有页面（含隧道来源一律 404）。
	for _, path := range []string{"/", "/dashboard", "/dashboard/"} {
		rr := httptest.NewRecorder()
		router.ServeHTTP(rr, localReq(path))
		if rr.Code != http.StatusNotFound {
			t.Fatalf("retired page %s: status = %d, want 404", path, rr.Code)
		}
	}
}
