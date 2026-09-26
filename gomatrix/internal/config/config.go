package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/BurntSushi/toml"
)

// AgentConfig defines a bot/agent account to auto-register on startup.
type AgentConfig struct {
	Name        string `toml:"name"`
	Password    string `toml:"password"`
	DisplayName string `toml:"display_name"`
	Default     bool   `toml:"default"`
}

// CoaraAPIConfig controls the /coara-api reverse proxy to the local coara
// web server (mobile module pages: usage/records/workflow/config).
type CoaraAPIConfig struct {
	Enabled      bool   `toml:"enabled"`
	UpstreamPort int    `toml:"upstream_port"`
	TokenFile    string `toml:"token_file"`
}

// PairingConfig：手机扫码配对的固定账号。密码非空时启用——启动时确保账号存在
// 并以配置为准重置密码；QR② 携带该账号凭据，手机重装后扫码找回同一账号与房间历史
type PairingConfig struct {
	Username string `toml:"username"`
	Password string `toml:"password"`
}

// RateLimitConfig throttles the credential endpoints (/login, /register)
// per source IP + account name, plus a coarser aggregate cap per source IP
// (IPPerMinute) so rotating account names cannot mint fresh buckets. /sync
// long polling and all other routes are never limited. Non-positive quotas
// disable the corresponding limiter.
type RateLimitConfig struct {
	Enabled           bool `toml:"enabled"`
	LoginPerMinute    int  `toml:"login_per_minute"`
	RegisterPerMinute int  `toml:"register_per_minute"`
	IPPerMinute       int  `toml:"ip_per_minute"`
	Burst             int  `toml:"burst"`
}

// Config holds all homeserver configuration.
type Config struct {
	ServerName         string          `toml:"server_name"`
	DatabasePath       string          `toml:"database_path"`
	Address            string          `toml:"address"`
	Port               int             `toml:"port"`
	MaxRequestSize     int64           `toml:"max_request_size"`
	AllowRegistration  bool            `toml:"allow_registration"`
	AllowFederation    bool            `toml:"allow_federation"`
	AllowEncryption    bool            `toml:"allow_encryption"`
	DefaultRoomVersion string          `toml:"default_room_version"`
	MediaPath          string          `toml:"media_path"`
	LogLevel           string          `toml:"log_level"`
	LogFile            string          `toml:"log_file"`
	Agents             []AgentConfig   `toml:"agents"`
	Tunnel             TunnelConfig    `toml:"tunnel"`
	CoaraAPI           CoaraAPIConfig  `toml:"coara_api"`
	Pairing            PairingConfig   `toml:"pairing"`
	RateLimit          RateLimitConfig `toml:"rate_limit"`
}

// Default returns a reasonable default configuration.
func Default() Config {
	return Config{
		ServerName:         "coara.local",
		DatabasePath:       "./gomatrix.db",
		Address:            "127.0.0.1",
		Port:               8008,
		MaxRequestSize:     20 * 1024 * 1024,
		AllowRegistration:  false,
		AllowFederation:    false,
		AllowEncryption:    false,
		DefaultRoomVersion: "10",
		MediaPath:          "./media",
		LogLevel:           "info",
		Agents:             DefaultAgents(),
		Tunnel:             DefaultTunnel(),
		CoaraAPI:           CoaraAPIConfig{Enabled: true, UpstreamPort: 8080},
		Pairing:            PairingConfig{Username: "phone"},
		RateLimit:          RateLimitConfig{Enabled: true, LoginPerMinute: 10, RegisterPerMinute: 5, IPPerMinute: 30, Burst: 3},
	}
}

// DefaultAgents returns the default agent configuration with coara as default.
func DefaultAgents() []AgentConfig {
	return []AgentConfig{
		{Name: "coara", Password: "", DisplayName: "coara", Default: true},
	}
}

// Load reads configuration from a TOML file and overlays GOMAX_* environment variables.
func Load(path string) (*Config, error) {
	cfg := Default()

	if _, err := os.Stat(path); err == nil {
		if _, err := toml.DecodeFile(path, &cfg); err != nil {
			return nil, fmt.Errorf("failed to decode config %s: %w", path, err)
		}
	} else if !os.IsNotExist(err) {
		return nil, fmt.Errorf("failed to stat config %s: %w", path, err)
	}

	// Minimal env overlay: GOMAX_SERVER_NAME, GOMAX_DATABASE_PATH, etc.
	overlayString("GOMAX_SERVER_NAME", &cfg.ServerName)
	overlayString("GOMAX_DATABASE_PATH", &cfg.DatabasePath)
	overlayString("GOMAX_ADDRESS", &cfg.Address)
	overlayInt("GOMAX_PORT", &cfg.Port)
	overlayInt64("GOMAX_MAX_REQUEST_SIZE", &cfg.MaxRequestSize)
	overlayBool("GOMAX_ALLOW_REGISTRATION", &cfg.AllowRegistration)
	overlayBool("GOMAX_ALLOW_FEDERATION", &cfg.AllowFederation)
	overlayBool("GOMAX_ALLOW_ENCRYPTION", &cfg.AllowEncryption)
	overlayString("GOMAX_DEFAULT_ROOM_VERSION", &cfg.DefaultRoomVersion)
	overlayString("GOMAX_MEDIA_PATH", &cfg.MediaPath)
	overlayString("GOMAX_LOG_LEVEL", &cfg.LogLevel)
	overlayBool("GOMAX_TUNNEL_ENABLED", &cfg.Tunnel.Enabled)
	overlayString("GOMAX_TUNNEL_MODE", &cfg.Tunnel.Mode)
	overlayString("GOMAX_TUNNEL_TOKEN", &cfg.Tunnel.Token)
	overlayString("GOMAX_TUNNEL_PUBLIC_URL", &cfg.Tunnel.PublicURL)
	overlayString("GOMAX_TUNNEL_BINARY_PATH", &cfg.Tunnel.BinaryPath)
	overlayBool("GOMAX_COARA_API_ENABLED", &cfg.CoaraAPI.Enabled)
	overlayInt("GOMAX_COARA_API_PORT", &cfg.CoaraAPI.UpstreamPort)
	overlayString("GOMAX_COARA_API_TOKEN_FILE", &cfg.CoaraAPI.TokenFile)
	overlayBool("GOMAX_RATE_LIMIT_ENABLED", &cfg.RateLimit.Enabled)
	overlayInt("GOMAX_RATE_LIMIT_LOGIN_PER_MINUTE", &cfg.RateLimit.LoginPerMinute)
	overlayInt("GOMAX_RATE_LIMIT_REGISTER_PER_MINUTE", &cfg.RateLimit.RegisterPerMinute)
	overlayInt("GOMAX_RATE_LIMIT_BURST", &cfg.RateLimit.Burst)

	cfg.ServerName = strings.ToLower(cfg.ServerName)
	if cfg.Tunnel.Mode == "" {
		cfg.Tunnel.Mode = "quick"
	}
	if cfg.ServerName == "" {
		return nil, fmt.Errorf("server_name must be configured")
	}

	cfg.anchorPaths(path)

	return &cfg, nil
}

// anchorPaths resolves relative data paths against the config file's
// directory, so the database and media store no longer depend on the
// process working directory (Linux .desktop Path= is often ignored).
func (c *Config) anchorPaths(configPath string) {
	dir, err := filepath.Abs(filepath.Dir(configPath))
	if err != nil {
		return
	}
	c.DatabasePath = anchorPath(c.DatabasePath, dir)
	c.MediaPath = anchorPath(c.MediaPath, dir)
}

// anchorPath returns p resolved against baseDir. Compatibility fallback: if
// the anchored path does not exist but the CWD-relative path does (legacy
// pre-data-dir layout), keep the CWD-relative path so existing deployments
// do not silently switch to a fresh, empty database.
func anchorPath(p, baseDir string) string {
	if p == "" || filepath.IsAbs(p) {
		return p
	}
	anchored := filepath.Join(baseDir, p)
	if _, err := os.Stat(anchored); err == nil {
		return anchored
	}
	if _, err := os.Stat(p); err == nil {
		return p
	}
	return anchored
}

func overlayString(key string, target *string) {
	if v := os.Getenv(key); v != "" {
		*target = v
	}
}

func overlayInt(key string, target *int) {
	if v := os.Getenv(key); v != "" {
		fmt.Sscanf(v, "%d", target)
	}
}

func overlayInt64(key string, target *int64) {
	if v := os.Getenv(key); v != "" {
		fmt.Sscanf(v, "%d", target)
	}
}

func overlayBool(key string, target *bool) {
	if v := os.Getenv(key); v != "" {
		switch strings.ToLower(v) {
		case "true", "1", "yes", "on":
			*target = true
		case "false", "0", "no", "off":
			*target = false
		}
	}
}
