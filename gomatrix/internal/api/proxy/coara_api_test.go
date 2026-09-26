package proxy

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeTokenFile(t *testing.T, token string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "dashboard_token")
	if err := os.WriteFile(path, []byte(token+"\n"), 0o600); err != nil {
		t.Fatalf("write token file: %v", err)
	}
	return path
}

func TestProxyRewritesPathAndInjectsToken(t *testing.T) {
	var gotPath, gotQuery, gotToken string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		gotToken = r.Header.Get("X-Coara-Token")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer upstream.Close()

	p := newCoaraAPIProxy(strings.TrimPrefix(upstream.URL, "http://"), writeTokenFile(t, "secret-token"))

	// chi v5 Mount 不改写裸 http.Handler 看到的 req.URL.Path，
	// 真实请求带完整 /coara-api 前缀到达，Director 自行剥离
	req := httptest.NewRequest(http.MethodGet, "/coara-api/usage/dashboard?days=7", nil)
	rr := httptest.NewRecorder()
	p.ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body: %s)", rr.Code, rr.Body.String())
	}
	if gotPath != "/api/usage/dashboard" {
		t.Fatalf("upstream path = %q, want /api/usage/dashboard", gotPath)
	}
	if gotQuery != "days=7" {
		t.Fatalf("upstream query = %q, want days=7", gotQuery)
	}
	if gotToken != "secret-token" {
		t.Fatalf("upstream X-Coara-Token = %q, want secret-token", gotToken)
	}
}

func TestProxyReloadsTokenOnMtimeChange(t *testing.T) {
	tokenPath := writeTokenFile(t, "token-v1")
	var gotToken string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotToken = r.Header.Get("X-Coara-Token")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer upstream.Close()

	p := newCoaraAPIProxy(strings.TrimPrefix(upstream.URL, "http://"), tokenPath)
	p.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/x", nil))
	if gotToken != "token-v1" {
		t.Fatalf("first token = %q, want token-v1", gotToken)
	}

	// Rewrite the token with a newer mtime; proxy must pick it up.
	if err := os.WriteFile(tokenPath, []byte("token-v2"), 0o600); err != nil {
		t.Fatalf("rewrite token: %v", err)
	}
	newer := time.Now().Add(2 * time.Second)
	if err := os.Chtimes(tokenPath, newer, newer); err != nil {
		t.Fatalf("chtimes: %v", err)
	}
	p.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/x", nil))
	if gotToken != "token-v2" {
		t.Fatalf("reloaded token = %q, want token-v2", gotToken)
	}
}

func TestProxyUnavailableWhenUpstreamDown(t *testing.T) {
	p := newCoaraAPIProxy("127.0.0.1:1", writeTokenFile(t, "secret-token"))
	rr := httptest.NewRecorder()
	p.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/usage/dashboard", nil))

	if rr.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502", rr.Code)
	}
	var body map[string]string
	if err := json.Unmarshal(rr.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if body["error"] != "pc_unreachable" {
		t.Fatalf("error = %q, want pc_unreachable", body["error"])
	}
}

func TestProxyUnavailableWhenTokenFileMissing(t *testing.T) {
	p := newCoaraAPIProxy("127.0.0.1:1", filepath.Join(t.TempDir(), "nope"))
	rr := httptest.NewRecorder()
	p.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/usage/dashboard", nil))

	if rr.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", rr.Code)
	}
	var body map[string]string
	if err := json.Unmarshal(rr.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if body["error"] != "dashboard_token_unavailable" {
		t.Fatalf("error = %q, want dashboard_token_unavailable", body["error"])
	}
}
