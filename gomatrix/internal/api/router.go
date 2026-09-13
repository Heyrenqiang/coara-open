package api

import (
	"log/slog"
	"net/http"
	"net/url"
	"strings"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/api/client"
	"gomatrix/internal/api/federation"
	"gomatrix/internal/api/proxy"
	"gomatrix/internal/dashboard"
	"gomatrix/internal/service"
	"gomatrix/internal/tunnel"
)

// clientAPIVersions lists supported Client-Server API path prefixes.
var clientAPIVersions = []string{"v3", "r0"}

func clientPrefix(version, suffix string) string {
	return "/_matrix/client/" + version + suffix
}

func mountClientGET(r chi.Router, suffix string, handler http.HandlerFunc) {
	for _, ver := range clientAPIVersions {
		r.Get(clientPrefix(ver, suffix), handler)
	}
}

func mountClientPOST(r chi.Router, suffix string, handler http.HandlerFunc) {
	for _, ver := range clientAPIVersions {
		r.Post(clientPrefix(ver, suffix), handler)
	}
}

func mountClientPUT(r chi.Router, suffix string, handler http.HandlerFunc) {
	for _, ver := range clientAPIVersions {
		r.Put(clientPrefix(ver, suffix), handler)
	}
}

func mountClientDELETE(r chi.Router, suffix string, handler http.HandlerFunc) {
	for _, ver := range clientAPIVersions {
		r.Delete(clientPrefix(ver, suffix), handler)
	}
}

// NewRouter builds the full HTTP router.
func NewRouter() http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.Recoverer)
	r.Use(middleware.RealIP)
	r.Use(loggingMiddleware)
	r.Use(corsMiddleware)

	// Client-Server API (unauthenticated first). Login/register carry tight
	// per source IP + account rate limits plus a coarser aggregate cap per
	// source IP, so account-name rotation cannot mint fresh buckets; /sync
	// long polling and every other route deliberately stay unlimited.
	rlCfg := service.Global().Config.RateLimit
	var loginLimiter, registerLimiter, loginIPLimiter, registerIPLimiter *RateLimiter
	if rlCfg.Enabled {
		loginLimiter = NewRateLimiter(rlCfg.LoginPerMinute, rlCfg.Burst)
		registerLimiter = NewRateLimiter(rlCfg.RegisterPerMinute, rlCfg.Burst)
		// The aggregate per-IP bucket bursts up to its full per-minute
		// quota so a few legitimate clients behind one NAT IP keep enough
		// headroom while floods are still capped.
		loginIPLimiter = NewRateLimiter(rlCfg.IPPerMinute, rlCfg.IPPerMinute)
		registerIPLimiter = NewRateLimiter(rlCfg.IPPerMinute, rlCfg.IPPerMinute)
	}
	r.Group(func(r chi.Router) {
		r.Use(authRateLimit(registerLimiter, registerIPLimiter))
		mountClientPOST(r, "/register", client.Register)
	})
	r.Group(func(r chi.Router) {
		r.Use(authRateLimit(loginLimiter, loginIPLimiter))
		mountClientPOST(r, "/login", client.Login)
	})
	mountClientPOST(r, "/logout", client.Logout)
	mountClientGET(r, "/capabilities", client.Capabilities)
	mountClientGET(r, "/versions", client.Versions)
	r.Get("/_matrix/client/versions", client.Versions)

	// Well-known
	r.Get("/.well-known/matrix/client", client.WellKnown)
	r.Get("/.well-known/matrix/server", federation.WellKnown)

	// Agent discovery (unauthenticated - used by mobile app before login)
	r.Get("/api/agents", client.Agents)

	// Authenticated client routes
	r.Group(func(r chi.Router) {
		r.Use(AuthMiddleware)

		mountClientGET(r, "/account/whoami", client.Whoami)

		mountClientGET(r, "/profile/{userId}/displayname", client.GetDisplayName)
		mountClientPUT(r, "/profile/{userId}/displayname", client.SetDisplayName)
		mountClientGET(r, "/profile/{userId}/avatar_url", client.GetAvatarURL)
		mountClientPUT(r, "/profile/{userId}/avatar_url", client.SetAvatarURL)

		mountClientGET(r, "/devices", client.GetDevices)
		mountClientPUT(r, "/devices/{deviceId}", client.UpdateDevice)
		mountClientDELETE(r, "/devices/{deviceId}", client.DeleteDevice)

		mountClientPOST(r, "/keys/upload", client.KeysUpload)
		mountClientPOST(r, "/keys/query", client.KeysQuery)
		mountClientPOST(r, "/keys/claim", client.KeysClaim)

		mountClientPOST(r, "/createRoom", client.CreateRoom)
		mountClientPOST(r, "/join/{roomIdOrAlias}", client.JoinRoom)
		mountClientPOST(r, "/rooms/{roomId}/invite", client.Invite)
		mountClientPOST(r, "/rooms/{roomId}/join", client.JoinRoomByID)
		mountClientPOST(r, "/rooms/{roomId}/leave", client.Leave)
		mountClientPOST(r, "/rooms/{roomId}/forget", client.Forget)

		mountClientGET(r, "/directory/room/{roomAlias}", client.ResolveAlias)
		mountClientPUT(r, "/directory/room/{roomAlias}", client.SetAlias)
		mountClientDELETE(r, "/directory/room/{roomAlias}", client.DeleteAlias)

		mountClientGET(r, "/rooms/{roomId}/state", client.GetState)
		mountClientGET(r, "/rooms/{roomId}/state/{eventType}/{stateKey}", client.GetStateEvent)
		mountClientPUT(r, "/rooms/{roomId}/state/{eventType}/{stateKey}", client.SetStateEvent)
		mountClientGET(r, "/rooms/{roomId}/messages", client.GetMessages)
		mountClientPUT(r, "/rooms/{roomId}/send/{eventType}/{txnId}", client.SendMessage)
		mountClientGET(r, "/joined_rooms", client.JoinedRooms)
		mountClientGET(r, "/rooms/{roomId}/members", client.RoomMembers)
		mountClientPUT(r, "/rooms/{roomId}/typing/{userId}", client.SetTyping)
		mountClientGET(r, "/sync", client.Sync)

		// coara REST API reverse proxy (mobile module pages: usage/records/
		// workflow/config). Injects the dashboard token upstream, so clients
		// need only their Matrix access token.
		if ca := service.Global().Config.CoaraAPI; ca.Enabled {
			r.Mount("/coara-api", proxy.NewCoaraAPIProxy(ca.UpstreamPort, ca.TokenFile))
			// 兜底反代：gomatrix 自身未认领的一切路径（SPA / 静态资源 / /ws /
			// /api/*）原样透传到 coara web server——手机端的工作流编辑器
			// 经它加载。chi 按最长匹配优先，/coara-api、/api/agents、/_matrix
			// 等已注册路由不受影响。
			r.Mount("/", proxy.NewCoaraWebProxy(ca.UpstreamPort, ca.TokenFile))
		}
	})

	// Media (v3 + r0 compatibility)
	for _, ver := range []string{"v3", "r0"} {
		r.With(AuthMiddleware).Post("/_matrix/media/"+ver+"/upload", client.UploadMedia)
		r.Get("/_matrix/media/"+ver+"/download/{serverName}/{mediaId}", client.DownloadMedia)
		r.Get("/_matrix/media/"+ver+"/thumbnail/{serverName}/{mediaId}", client.ClientThumbnailMedia)
	}

	// Client-Server authenticated media (matrix-nio default download path)
	r.Get("/_matrix/client/v1/media/download/{serverName}/{mediaId}", client.ClientDownloadMedia)
	r.Get("/_matrix/client/v1/media/thumbnail/{serverName}/{mediaId}", client.ClientThumbnailMedia)

	// Federation (optional). The transaction endpoint accepts PDUs without
	// signature verification, so only mount it when federation is enabled;
	// the key endpoint is likewise only meaningful to federation peers.
	if service.Global().Config.AllowFederation {
		slog.Warn("federation enabled: signature verification NOT implemented — for testing only, do not use in production")
		r.Get("/_matrix/key/v2/server", federation.ServerKeys)
		r.Post("/_matrix/federation/v1/send/{txnId}", federation.ReceiveTransaction)
	}

	// 数据接口（coara WebUI 的手机接入页从这里取配对二维码与连接状态）。
	// Restricted to direct loopback/LAN clients: anything relayed by the
	// Cloudflare tunnel (CF-* headers) or arriving from a non-private
	// address is rejected, so the tunnel no longer exposes LAN IPs, the
	// tunnel URL, or the pairing QR code.
	r.Group(func(r chi.Router) {
		r.Use(dashboardLocalOnly)
		r.Get("/dashboard/qr.png", dashboard.ServeQRCode)
		r.Get("/api/dashboard/status", dashboard.ServeStatus)
	})

	// Fallback for unimplemented endpoints
	r.NotFound(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/_matrix/") {
			apiutil.WriteMatrixError(w, apiutil.ErrMUnrecognized, "Unrecognized request", http.StatusNotFound)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	})

	return r
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ww := middleware.NewWrapResponseWriter(w, r.ProtoMajor)
		next.ServeHTTP(ww, r)
		slog.Debug("http request", "method", r.Method, "path", r.URL.Path, "status", ww.Status(), "size", ww.BytesWritten())
	})
}

func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Requests without an Origin header (same-origin dashboard, mobile
		// apps, CLI clients) are unaffected by CORS and always work.
		if origin := r.Header.Get("Origin"); origin != "" && isAllowedOrigin(origin) {
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Set("Vary", "Origin")
		}
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Origin, X-Requested-With, Content-Type, Accept, Authorization")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusOK)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// isAllowedOrigin permits local web UIs (localhost dev servers) and the
// configured tunnel public URL; everything else is rejected.
func isAllowedOrigin(origin string) bool {
	u, err := url.Parse(origin)
	if err != nil {
		return false
	}
	host := u.Hostname()
	if host == "localhost" || host == "127.0.0.1" || host == "::1" {
		return true
	}
	if s := service.Global(); s != nil && s.Config.Tunnel.PublicURL != "" {
		if pu, err := url.Parse(s.Config.Tunnel.PublicURL); err == nil && strings.EqualFold(pu.Hostname(), host) {
			return true
		}
	}
	if m := tunnel.Global(); m != nil && m.PublicURL() != "" {
		if pu, err := url.Parse(m.PublicURL()); err == nil && strings.EqualFold(pu.Hostname(), host) {
			return true
		}
	}
	return false
}
