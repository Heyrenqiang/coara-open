package connect

import (
	"encoding/json"
	"fmt"
	"strings"

	"gomatrix/internal/config"
)

// QRPayload is encoded in the mobile setup QR code.
type QRPayload struct {
	HomeserverURL string `json:"m.server"`
	ServerName    string `json:"coara.server_name"`
	BotUser       string `json:"coara.bot_user"`
	Tunnel        string `json:"coara.tunnel"`
	// 固定配对账号凭据（[pairing] 配置启用时携带）：手机重装后扫码找回同一账号与房间
	PairUser          string `json:"m.user,omitempty"`
	PairPassword      string `json:"m.password,omitempty"`
	RegistrationToken string `json:"coara.registration_token,omitempty"`
}

// EncodeQRPayload returns JSON for the coara App connect QR code.
// pairUser/pairPassword 为空则不携带配对凭据（App 退回随机注册）
func EncodeQRPayload(tunnelURL, serverName, botUser, pairUser, pairPassword, registrationToken string) string {
	payload := QRPayload{
		HomeserverURL:     strings.TrimRight(tunnelURL, "/"),
		ServerName:        serverName,
		BotUser:           botUser,
		Tunnel:            "true",
		PairUser:          pairUser,
		PairPassword:      pairPassword,
		RegistrationToken: registrationToken,
	}
	data, _ := json.Marshal(payload)
	return string(data)
}

// DefaultBotUser returns the default agent Matrix user ID for QR payloads.
func DefaultBotUser(cfg *config.Config) string {
	for _, agent := range cfg.Agents {
		if agent.Default {
			return fmt.Sprintf("@%s:%s", strings.ToLower(strings.TrimSpace(agent.Name)), cfg.ServerName)
		}
	}
	if len(cfg.Agents) > 0 {
		name := strings.ToLower(strings.TrimSpace(cfg.Agents[0].Name))
		return fmt.Sprintf("@%s:%s", name, cfg.ServerName)
	}
	return fmt.Sprintf("@coara:%s", cfg.ServerName)
}
