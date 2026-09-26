package api

import (
	"net"
	"net/http"
)

// dashboardLocalOnly serves dashboard endpoints only to direct loopback/LAN
// clients. Requests relayed by the Cloudflare tunnel always carry CF-* edge
// headers (RemoteAddr alone shows 127.0.0.1 because cloudflared dials the
// server locally), so checking only RemoteAddr would not stop them. Anything
// tunnel-originated or from a non-private address is rejected before it can
// leak LAN IPs, the tunnel URL, or the pairing QR code.
func dashboardLocalOnly(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !isDirectLocalClient(r) {
			http.Error(w, "dashboard is only available to local clients", http.StatusForbidden)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// isDirectLocalClient reports whether the request is a direct local
// connection: no Cloudflare tunnel headers and a loopback/private/link-local
// peer address.
func isDirectLocalClient(r *http.Request) bool {
	if r.Header.Get("CF-Connecting-IP") != "" ||
		r.Header.Get("CF-Ray") != "" ||
		r.Header.Get("CF-Visitor") != "" {
		return false
	}
	ip := net.ParseIP(remoteIP(r))
	if ip == nil {
		return false
	}
	return ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast()
}
