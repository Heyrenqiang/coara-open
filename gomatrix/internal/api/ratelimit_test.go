package api

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"gomatrix/internal/config"
	"gomatrix/internal/db"
	"gomatrix/internal/service"
)

func TestRateLimiterTokenBucket(t *testing.T) {
	l := NewRateLimiter(60, 2)
	if l == nil {
		t.Fatal("NewRateLimiter returned nil for positive quota")
	}
	if ok, _ := l.Allow("k"); !ok {
		t.Fatal("first request denied, want allowed")
	}
	if ok, _ := l.Allow("k"); !ok {
		t.Fatal("second request denied, want allowed (burst=2)")
	}
	ok, retryAfter := l.Allow("k")
	if ok {
		t.Fatal("third request allowed, want denied (bucket empty)")
	}
	if retryAfter <= 0 {
		t.Fatalf("retryAfter = %v, want positive", retryAfter)
	}
	// Independent keys get independent buckets.
	if ok, _ := l.Allow("other"); !ok {
		t.Fatal("other key denied, want independent bucket")
	}
	// 60/min = 1 token/s: after a refill interval the key recovers.
	time.Sleep(1100 * time.Millisecond)
	if ok, _ := l.Allow("k"); !ok {
		t.Fatal("request after refill denied, want allowed")
	}
}

func TestRateLimiterNonPositiveQuotaDisabled(t *testing.T) {
	if NewRateLimiter(0, 1) != nil || NewRateLimiter(1, 0) != nil || NewRateLimiter(-5, 3) != nil {
		t.Fatal("non-positive quota must yield nil limiter")
	}
	var nilLimiter *RateLimiter
	if ok, _ := nilLimiter.Allow("k"); !ok {
		t.Fatal("nil limiter must allow everything")
	}
}

// TestLoginRateLimitedPerIPAndAccount drives the real router with a 1/min
// login quota and verifies 429 + Retry-After semantics, per IP+account
// bucketing, and that /sync long polling is never limited.
func TestLoginRateLimitedPerIPAndAccount(t *testing.T) {
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
		RateLimit:          config.RateLimitConfig{Enabled: true, LoginPerMinute: 1, RegisterPerMinute: 1, Burst: 1},
	}
	database, err := db.Open(cfg)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() { database.Close() })
	service.SetGlobal(service.Build(cfg, database))

	router := NewRouter()
	login := func(remoteAddr, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/_matrix/client/v3/login", strings.NewReader(body))
		req.RemoteAddr = remoteAddr
		rr := httptest.NewRecorder()
		router.ServeHTTP(rr, req)
		return rr
	}

	// First attempt consumes the single burst token and reaches the handler
	// (403 invalid credentials), it is not throttled.
	if rr := login("203.0.113.7:5555", `{"user":"alice","password":"wrong"}`); rr.Code != http.StatusForbidden {
		t.Fatalf("first login: status = %d, want 403 (handler reached, body: %s)", rr.Code, rr.Body.String())
	}

	// Second attempt from the same IP + account is throttled.
	rr := login("203.0.113.7:5555", `{"user":"alice","password":"wrong"}`)
	if rr.Code != http.StatusTooManyRequests {
		t.Fatalf("second login: status = %d, want 429 (body: %s)", rr.Code, rr.Body.String())
	}
	if rr.Header().Get("Retry-After") == "" {
		t.Fatal("429 response missing Retry-After header")
	}
	if !strings.Contains(rr.Body.String(), "M_LIMIT_EXCEEDED") {
		t.Fatalf("429 body = %s, want M_LIMIT_EXCEEDED errcode", rr.Body.String())
	}

	// A different account from the same IP gets its own bucket.
	if rr := login("203.0.113.7:5555", `{"user":"bob","password":"wrong"}`); rr.Code != http.StatusForbidden {
		t.Fatalf("other account: status = %d, want 403", rr.Code)
	}
	// The same account from a different IP gets its own bucket.
	if rr := login("198.51.100.9:5555", `{"user":"alice","password":"wrong"}`); rr.Code != http.StatusForbidden {
		t.Fatalf("other IP: status = %d, want 403", rr.Code)
	}

	// /sync long polling is never rate limited, even after the login bucket
	// is exhausted: missing token yields 401 from AuthMiddleware, not 429.
	req := httptest.NewRequest(http.MethodGet, "/_matrix/client/v3/sync", nil)
	req.RemoteAddr = "203.0.113.7:5555"
	rr = httptest.NewRecorder()
	router.ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("/sync: status = %d, want 401 (never throttled)", rr.Code)
	}

	// Registration has its own limiter and is also throttled.
	register := func() *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/_matrix/client/v3/register",
			strings.NewReader(`{"username":"carol","password":"x"}`))
		req.RemoteAddr = "203.0.113.7:5555"
		rr := httptest.NewRecorder()
		router.ServeHTTP(rr, req)
		return rr
	}
	if rr := register(); rr.Code == http.StatusTooManyRequests {
		t.Fatal("first register throttled, want handler reached")
	}
	if rr := register(); rr.Code != http.StatusTooManyRequests {
		t.Fatalf("second register: status = %d, want 429", rr.Code)
	}
}

// newRateLimitTestRouter builds a real router against a throwaway DB with
// the given rate-limit config.
func newRateLimitTestRouter(t *testing.T, rl config.RateLimitConfig) http.Handler {
	t.Helper()
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
		RateLimit:          rl,
	}
	database, err := db.Open(cfg)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() { database.Close() })
	service.SetGlobal(service.Build(cfg, database))
	return NewRouter()
}

// postCredential issues one login/register POST through the router.
func postCredential(t *testing.T, router http.Handler, path, remoteAddr string, headers map[string]string, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	req.RemoteAddr = remoteAddr
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	rr := httptest.NewRecorder()
	router.ServeHTTP(rr, req)
	return rr
}

// TestLoginRateLimitTunnelKeyedByCFConnectingIP verifies that requests
// relayed by the Cloudflare tunnel (CF-Ray present) are bucketed by the
// edge-written CF-Connecting-IP, so forging the leftmost X-Forwarded-For
// value cannot mint a fresh bucket.
func TestLoginRateLimitTunnelKeyedByCFConnectingIP(t *testing.T) {
	router := newRateLimitTestRouter(t, config.RateLimitConfig{Enabled: true, LoginPerMinute: 1, Burst: 1})
	login := func(headers map[string]string) *httptest.ResponseRecorder {
		return postCredential(t, router, "/_matrix/client/v3/login", "127.0.0.1:4100", headers,
			`{"user":"alice","password":"wrong"}`)
	}
	// cloudflared dials the server locally, so RemoteAddr is loopback and
	// middleware.RealIP folds the client-controlled leftmost XFF into it.
	cf := map[string]string{
		"CF-Ray":           "9000000000000001-SIN",
		"CF-Connecting-IP": "203.0.113.7",
		"X-Forwarded-For":  "1.1.1.1, 203.0.113.7",
	}
	if rr := login(cf); rr.Code != http.StatusForbidden {
		t.Fatalf("first tunnel login: status = %d, want 403 (handler reached, body: %s)", rr.Code, rr.Body.String())
	}
	// Same real client forging a different leftmost XFF stays in the bucket
	// keyed by CF-Connecting-IP and is throttled.
	cf["X-Forwarded-For"] = "2.2.2.2, 203.0.113.7"
	rr := login(cf)
	if rr.Code != http.StatusTooManyRequests {
		t.Fatalf("tunnel login with forged XFF: status = %d, want 429 (XFF spoof must not mint a fresh bucket)", rr.Code)
	}
	if rr.Header().Get("Retry-After") == "" {
		t.Fatal("429 response missing Retry-After header")
	}
	// A different edge-written client IP gets its own bucket.
	cf["CF-Connecting-IP"] = "198.51.100.9"
	if rr := login(cf); rr.Code != http.StatusForbidden {
		t.Fatalf("other tunnel client: status = %d, want 403", rr.Code)
	}
}

// TestLoginRateLimitCFConnectingIPIgnoredWithoutCFRay verifies the direct
// form keeps the existing RealIP semantics: without CF-Ray a bare
// client-supplied CF-Connecting-IP is not trusted as the bucket key.
func TestLoginRateLimitCFConnectingIPIgnoredWithoutCFRay(t *testing.T) {
	router := newRateLimitTestRouter(t, config.RateLimitConfig{Enabled: true, LoginPerMinute: 1, Burst: 1})
	login := func(headers map[string]string) *httptest.ResponseRecorder {
		return postCredential(t, router, "/_matrix/client/v3/login", "203.0.113.7:5555", headers,
			`{"user":"alice","password":"wrong"}`)
	}
	if rr := login(map[string]string{"CF-Connecting-IP": "1.1.1.1"}); rr.Code != http.StatusForbidden {
		t.Fatalf("first direct login: status = %d, want 403", rr.Code)
	}
	// No CF-Ray: the spoofed CF-Connecting-IP must not change the bucket,
	// so the second attempt from the same peer is throttled.
	if rr := login(map[string]string{"CF-Connecting-IP": "2.2.2.2"}); rr.Code != http.StatusTooManyRequests {
		t.Fatalf("direct login with spoofed CF-Connecting-IP: status = %d, want 429 (bare CF-Connecting-IP must not be trusted)", rr.Code)
	}
}

// TestRateLimitPerIPAggregateBucket verifies the aggregate per-IP bucket:
// rotating account names from one source IP cannot mint fresh buckets, for
// both /login and /register.
func TestRateLimitPerIPAggregateBucket(t *testing.T) {
	// Generous per-account quotas; only the aggregate per-IP cap (2/min,
	// burst = quota) may bite.
	router := newRateLimitTestRouter(t, config.RateLimitConfig{
		Enabled: true, LoginPerMinute: 100, RegisterPerMinute: 100, IPPerMinute: 2, Burst: 100,
	})
	login := func(user string) *httptest.ResponseRecorder {
		return postCredential(t, router, "/_matrix/client/v3/login", "203.0.113.7:5555", nil,
			`{"user":"`+user+`","password":"wrong"}`)
	}
	if rr := login("alice"); rr.Code != http.StatusForbidden {
		t.Fatalf("login alice: status = %d, want 403", rr.Code)
	}
	if rr := login("bob"); rr.Code != http.StatusForbidden {
		t.Fatalf("login bob: status = %d, want 403", rr.Code)
	}
	// carol's per-account bucket is fresh, but the per-IP aggregate bucket
	// is exhausted.
	rr := login("carol")
	if rr.Code != http.StatusTooManyRequests {
		t.Fatalf("login carol: status = %d, want 429 (per-IP aggregate bucket exhausted)", rr.Code)
	}
	if rr.Header().Get("Retry-After") == "" {
		t.Fatal("aggregate 429 missing Retry-After header")
	}

	// Registration has its own aggregate bucket and is likewise capped.
	register := func(user string) *httptest.ResponseRecorder {
		return postCredential(t, router, "/_matrix/client/v3/register", "203.0.113.7:5555", nil,
			`{"username":"`+user+`","password":"x"}`)
	}
	if rr := register("dave"); rr.Code == http.StatusTooManyRequests {
		t.Fatal("register dave throttled, want handler reached")
	}
	if rr := register("erin"); rr.Code == http.StatusTooManyRequests {
		t.Fatal("register erin throttled, want handler reached")
	}
	if rr := register("frank"); rr.Code != http.StatusTooManyRequests {
		t.Fatalf("register frank: status = %d, want 429 (per-IP aggregate bucket exhausted)", rr.Code)
	}

	// A different source IP still gets its own aggregate bucket.
	if rr := postCredential(t, router, "/_matrix/client/v3/login", "198.51.100.9:5555", nil,
		`{"user":"alice","password":"wrong"}`); rr.Code != http.StatusForbidden {
		t.Fatalf("login from other IP: status = %d, want 403 (independent aggregate bucket)", rr.Code)
	}
}

// TestRateLimitDisabledByDefault confirms that a config without an explicit
// [rate_limit] section (zero value, as in pre-existing configs built without
// Default()) preserves the old unlimited behavior.
func TestRateLimitDisabledByDefault(t *testing.T) {
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
	for i := 0; i < 20; i++ {
		req := httptest.NewRequest(http.MethodPost, "/_matrix/client/v3/login",
			strings.NewReader(`{"user":"dave","password":"wrong"}`))
		req.RemoteAddr = "203.0.113.7:5555"
		rr := httptest.NewRecorder()
		router.ServeHTTP(rr, req)
		if rr.Code == http.StatusTooManyRequests {
			t.Fatalf("attempt %d throttled with rate limiting disabled", i+1)
		}
	}
}
