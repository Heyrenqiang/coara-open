package dashboard

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"time"

	"github.com/skip2/go-qrcode"
	"gomatrix/internal/connect"
	"gomatrix/internal/service"
	"gomatrix/internal/tunnel"
)

var startTime = time.Now()

// DashboardStatus is the JSON response for GET /api/dashboard/status.
type DashboardStatus struct {
	ServerName     string              `json:"server_name"`
	Port           int                 `json:"port"`
	Running        bool                `json:"running"`
	UptimeSeconds  float64             `json:"uptime_seconds"`
	ConnectionURL  string              `json:"connection_url"`
	TunnelEnabled  bool                `json:"tunnel_enabled"`
	TunnelReady    bool                `json:"tunnel_ready"`
	TunnelState    string              `json:"tunnel_state,omitempty"`
	TunnelURL      string              `json:"tunnel_url"`
	TunnelError    string              `json:"tunnel_error,omitempty"`
	LocalURLs      []string            `json:"local_urls"`
	LocalhostURL   string              `json:"localhost_url"`
	Agents         []service.AgentInfo `json:"agents"`
	// PhoneConnected: at least one non-agent Matrix user synced recently (coara App).
	PhoneConnected bool `json:"phone_connected"`
}

// ServeQRCode generates and returns a PNG QR code encoding the mobile connect payload.
func ServeQRCode(w http.ResponseWriter, r *http.Request) {
	svc := service.Global()
	if svc == nil {
		http.Error(w, "service not initialized", http.StatusInternalServerError)
		return
	}

	url, payload, ok := mobileConnectPayload(svc)
	if !ok {
		if svc.Config.Tunnel.Enabled {
			http.Error(w, "Cloudflare tunnel not ready — wait a few seconds and refresh", http.StatusServiceUnavailable)
		} else {
			http.Error(w, "tunnel disabled — start gomatrix without --no-tunnel for mobile access", http.StatusServiceUnavailable)
		}
		return
	}

	_ = url // encoded inside JSON payload
	png, err := qrcode.Encode(payload, qrcode.Medium, 256)
	if err != nil {
		slog.Error("failed to generate QR code", "error", err)
		http.Error(w, "failed to generate QR code", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Cache-Control", "no-cache")
	_, _ = w.Write(png)
}

// ServeStatus returns server status as JSON.
func ServeStatus(w http.ResponseWriter, r *http.Request) {
	svc := service.Global()
	if svc == nil {
		http.Error(w, "service not initialized", http.StatusInternalServerError)
		return
	}

	port := svc.Config.Port
	localURLs := getLocalURLs(port)
	tunnelStatus := tunnelSnapshot(svc.Config.Tunnel.Enabled)
	connectionURL := tunnelStatus.PublicURL
	if connectionURL == "" && !svc.Config.Tunnel.Enabled {
		connectionURL = primaryURL(localURLs, port)
	}

	status := DashboardStatus{
		ServerName:     svc.Config.ServerName,
		Port:           port,
		Running:        true,
		UptimeSeconds:  time.Since(startTime).Seconds(),
		ConnectionURL:  connectionURL,
		TunnelEnabled:  svc.Config.Tunnel.Enabled,
		TunnelReady:    tunnelStatus.Ready,
		TunnelState:    tunnelStatus.State,
		TunnelURL:      tunnelStatus.PublicURL,
		TunnelError:    tunnelStatus.Error,
		LocalURLs:      localURLs,
		LocalhostURL:   fmt.Sprintf("http://127.0.0.1:%d", port),
		Agents:         service.ListAgents(svc),
		PhoneConnected: service.AnyPhoneClientConnected(svc),
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-cache")
	_ = json.NewEncoder(w).Encode(status)
}

// tunnelSnapshot reads the Manager's live status — the watchdog keeps it
// current (ready / reconnecting / error), so no local override is applied.
func tunnelSnapshot(enabled bool) tunnel.Status {
	if !enabled {
		return tunnel.Status{Enabled: false}
	}
	if mgr := tunnel.Global(); mgr != nil {
		return mgr.StatusSnapshot()
	}
	return tunnel.Status{Enabled: true, State: tunnel.StateStarting, Error: "tunnel starting"}
}

func mobileConnectPayload(svc *service.Services) (homeserverURL, qrPayload string, ok bool) {
	if !svc.Config.Tunnel.Enabled {
		return "", "", false
	}
	mgr := tunnel.Global()
	if mgr == nil {
		return "", "", false
	}
	publicURL := mgr.PublicURL()
	if publicURL == "" {
		return "", "", false
	}
	botUser := connect.DefaultBotUser(svc.Config)
	ticket, err := svc.IssueRegistrationTicket()
	if err != nil {
		slog.Error("failed to issue registration ticket", "error", err)
		return "", "", false
	}
	payload := connect.EncodeQRPayload(
		publicURL, svc.Config.ServerName, botUser,
		svc.Config.Pairing.Username, svc.Config.Pairing.Password, ticket,
	)
	return publicURL, payload, true
}

// getLocalURLs returns all LAN-accessible HTTP URLs for this server.
func getLocalURLs(port int) []string {
	var urls []string
	interfaces, err := net.Interfaces()
	if err != nil {
		return urls
	}
	for _, iface := range interfaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			if ipNet, ok := addr.(*net.IPNet); ok && ipNet.IP.To4() != nil {
				urls = append(urls, fmt.Sprintf("http://%s:%d", ipNet.IP.String(), port))
			}
		}
	}
	return urls
}

func primaryURL(localURLs []string, port int) string {
	if len(localURLs) > 0 {
		return localURLs[0]
	}
	return fmt.Sprintf("http://127.0.0.1:%d", port)
}
