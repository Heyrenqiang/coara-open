package config

// TunnelConfig configures Cloudflare tunnel for remote mobile access.
type TunnelConfig struct {
	// Enabled starts cloudflared with GoMatrix (default: true).
	Enabled bool `toml:"enabled"`
	// Mode is "quick" (temporary trycloudflare.com URL) or "named" (fixed hostname).
	Mode string `toml:"mode"`
	// Token is the Cloudflare tunnel token for named mode.
	Token string `toml:"token"`
	// PublicURL is the expected public hostname for named mode
	// (e.g. https://matrix.example.com). For quick mode this is auto-detected.
	PublicURL string `toml:"public_url"`
	// BinaryPath overrides the cloudflared executable path.
	BinaryPath string `toml:"binary_path"`
}

// DefaultTunnel returns the default tunnel configuration (enabled, quick mode).
func DefaultTunnel() TunnelConfig {
	return TunnelConfig{
		Enabled: true,
		Mode:    "quick",
	}
}
