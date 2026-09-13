package api

import (
	"net/http"
	"sync"
	"time"

	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
)

// lastSeen throttles UpdateLastSeen writes to at most one per device per minute.
var (
	lastSeenMu sync.Mutex
	lastSeenAt = make(map[string]time.Time)
)

const lastSeenInterval = time.Minute

// shouldUpdateLastSeen reports whether the device's last-seen record is stale
// enough to justify another write.
func shouldUpdateLastSeen(deviceID string) bool {
	lastSeenMu.Lock()
	defer lastSeenMu.Unlock()
	if t, ok := lastSeenAt[deviceID]; ok && time.Since(t) < lastSeenInterval {
		return false
	}
	lastSeenAt[deviceID] = time.Now()
	return true
}

// AuthMiddleware validates access tokens.
func AuthMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := r.URL.Query().Get("access_token")
		if token == "" {
			h := r.Header.Get("Authorization")
			if len(h) > 7 && h[:7] == "Bearer " {
				token = h[7:]
			}
		}
		if token == "" {
			apiutil.WriteMatrixError(w, apiutil.ErrMMissingToken, "Missing access token", http.StatusUnauthorized)
			return
		}

		userID, deviceID, err := service.Global().Users.GetUserByToken(token)
		if err != nil {
			apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
			return
		}
		if userID == "" {
			apiutil.WriteMatrixError(w, apiutil.ErrMUnknownToken, "Unknown access token", http.StatusUnauthorized)
			return
		}

		// Update last seen without blocking the request path, at most once
		// per device per minute.
		if shouldUpdateLastSeen(deviceID) {
			go func(deviceID, remoteAddr string) {
				_ = service.Global().Users.UpdateLastSeen(deviceID, remoteAddr)
			}(deviceID, r.RemoteAddr)
		}

		next.ServeHTTP(w, r.WithContext(apiutil.WithAuthUser(r.Context(), userID, deviceID)))
	})
}
