package api

import (
	"bytes"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"gomatrix/internal/api/apiutil"
)

// bucket is one token bucket tracked by RateLimiter.
type bucket struct {
	tokens  float64
	updated time.Time
}

// RateLimiter is a dependency-free per-key token bucket. A nil RateLimiter
// allows everything, so disabled/unconfigured rate limits degrade to the
// previous behavior.
type RateLimiter struct {
	rate      float64 // tokens per second
	burst     float64
	mu        sync.Mutex
	buckets   map[string]*bucket
	lastSweep time.Time
}

// rateLimitSweepInterval controls how often idle buckets are evicted so the
// map does not grow unboundedly under a registration flood.
const rateLimitSweepInterval = 10 * time.Minute

// NewRateLimiter returns a limiter allowing perMinute requests per key with
// the given burst. Non-positive quotas yield nil (limiting disabled).
func NewRateLimiter(perMinute, burst int) *RateLimiter {
	if perMinute <= 0 || burst <= 0 {
		return nil
	}
	return &RateLimiter{
		rate:    float64(perMinute) / 60.0,
		burst:   float64(burst),
		buckets: make(map[string]*bucket),
	}
}

// Allow consumes one token for key. When denied, retryAfter estimates the
// wait until the next token is available.
func (l *RateLimiter) Allow(key string) (allowed bool, retryAfter time.Duration) {
	if l == nil {
		return true, 0
	}
	now := time.Now()
	l.mu.Lock()
	defer l.mu.Unlock()
	if now.Sub(l.lastSweep) > rateLimitSweepInterval {
		l.sweep(now)
	}
	b := l.buckets[key]
	if b == nil {
		b = &bucket{tokens: l.burst, updated: now}
		l.buckets[key] = b
	}
	b.tokens += now.Sub(b.updated).Seconds() * l.rate
	if b.tokens > l.burst {
		b.tokens = l.burst
	}
	b.updated = now
	if b.tokens < 1 {
		return false, time.Duration((1 - b.tokens) / l.rate * float64(time.Second))
	}
	b.tokens--
	return true, 0
}

// sweep evicts buckets idle longer than rateLimitSweepInterval.
func (l *RateLimiter) sweep(now time.Time) {
	for k, b := range l.buckets {
		if now.Sub(b.updated) > rateLimitSweepInterval {
			delete(l.buckets, k)
		}
	}
	l.lastSweep = now
}

// authRateLimit limits credential endpoints (login/register) with two token
// buckets that must both pass: one per source IP + account name, and one
// aggregate per source IP so rotating account names cannot mint fresh
// buckets (registration floods, password spraying). Denied requests get 429
// + Retry-After with the Matrix M_LIMIT_EXCEEDED errcode. /sync long polling
// is never routed through this middleware.
func authRateLimit(perAccount, perIP *RateLimiter) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		if perAccount == nil && perIP == nil {
			return next
		}
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ip := rateLimitIP(r)
			allowed, retryAfter := perAccount.Allow(ip + "|" + authAccountName(r))
			ipAllowed, ipRetryAfter := perIP.Allow(ip)
			if !allowed || !ipAllowed {
				if ipRetryAfter > retryAfter {
					retryAfter = ipRetryAfter
				}
				w.Header().Set("Retry-After", strconv.Itoa(int(retryAfter.Seconds())+1))
				apiutil.WriteMatrixError(w, apiutil.ErrMLimitExceeded, "Too many requests — retry later", http.StatusTooManyRequests)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// rateLimitIP returns the client IP used as the rate-limit bucket key.
// Requests relayed by the Cloudflare tunnel always carry a CF-Ray header the
// client cannot suppress, and the edge overwrites CF-Connecting-IP with the
// true peer address, so that header is trusted exactly when CF-Ray is
// present; this stops tunnel clients from minting fresh buckets by forging
// the leftmost X-Forwarded-For value that middleware.RealIP folds into
// RemoteAddr (cloudflared dials the server locally, so RemoteAddr alone only
// shows loopback). Direct connections (no CF-Ray) keep the existing RealIP
// behavior. This mirrors the dashboard guard, which likewise treats CF-*
// headers as the tunnel marker.
func rateLimitIP(r *http.Request) string {
	if r.Header.Get("CF-Ray") != "" {
		if ip := net.ParseIP(strings.TrimSpace(r.Header.Get("CF-Connecting-IP"))); ip != nil {
			return ip.String()
		}
	}
	return remoteIP(r)
}

// remoteIP returns the client IP. The RealIP middleware has already folded
// forwarding headers into RemoteAddr upstream in the chain.
func remoteIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

// authAccountName peeks at the JSON body for the login/register username and
// restores the body for the real handler. Bodies beyond 1 MiB are truncated;
// the handler then rejects them as bad JSON (fail closed).
func authAccountName(r *http.Request) string {
	if r.Body == nil {
		return ""
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	_ = r.Body.Close()
	r.Body = io.NopCloser(bytes.NewReader(body))
	if err != nil || len(body) == 0 {
		return ""
	}
	var probe struct {
		User       string `json:"user"`
		Username   string `json:"username"`
		Identifier struct {
			User string `json:"user"`
		} `json:"identifier"`
	}
	if err := json.Unmarshal(body, &probe); err != nil {
		return ""
	}
	name := probe.User
	if name == "" {
		name = probe.Username
	}
	if name == "" {
		name = probe.Identifier.User
	}
	return strings.ToLower(name)
}
