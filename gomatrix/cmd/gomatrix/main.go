package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"gomatrix/internal/api"
	"gomatrix/internal/config"
	"gomatrix/internal/db"
	"gomatrix/internal/logging"
	"gomatrix/internal/service"
	"gomatrix/internal/tunnel"
)

// gomatrix 是 coara 的内嵌接入层（married sidecar）：只以无头服务模式运行，
// 由 coara 拉起/看护/关停。配对二维码与连接状态在 coara WebUI 展示
// （数据接口 /api/dashboard/status 与 /dashboard/qr.png 保留）。
func main() {
	noTunnel := flag.Bool("no-tunnel", false, "Disable Cloudflare tunnel (LAN-only dev mode)")
	configPath := flag.String("config", "", "Path to gomatrix.toml (overrides GOMAX_CONFIG env)")
	flag.Parse()

	cfgPath := *configPath
	if cfgPath == "" {
		cfgPath = os.Getenv("GOMAX_CONFIG")
	}
	if cfgPath == "" {
		cfgPath = "gomatrix.toml"
	}
	// Prefer gomatrix.toml next to the binary when CWD is wrong (common on Linux
	// when .desktop Path= is ignored and the user launches from the menu).
	if !filepath.IsAbs(cfgPath) {
		if exe, err := os.Executable(); err == nil {
			beside := filepath.Join(filepath.Dir(exe), filepath.Base(cfgPath))
			if _, err := os.Stat(beside); err == nil {
				if _, cwdErr := os.Stat(cfgPath); cwdErr != nil {
					cfgPath = beside
				}
			}
		}
	}

	cfg, err := config.Load(cfgPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to load config: %v\n", err)
		os.Exit(1)
	}
	if *noTunnel {
		cfg.Tunnel.Enabled = false
	}

	level := slog.LevelInfo
	switch cfg.LogLevel {
	case "debug":
		level = slog.LevelDebug
	case "warn":
		level = slog.LevelWarn
	case "error":
		level = slog.LevelError
	}
	if logCloser, err := logging.Setup(level, cfg.LogFile); err != nil {
		fmt.Fprintf(os.Stderr, "Failed to setup logging: %v\n", err)
		os.Exit(1)
	} else if logCloser != nil {
		defer logCloser.Close()
	}

	slog.Info("starting gomatrix",
		"server_name", cfg.ServerName,
		"listen", fmt.Sprintf("%s:%d", cfg.Address, cfg.Port),
		"tunnel", cfg.Tunnel.Enabled,
	)

	database, err := db.Open(cfg)
	if err != nil {
		slog.Error("failed to open database", "error", err)
		os.Exit(1)
	}
	defer database.Close()

	services := service.Build(cfg, database)
	service.SetGlobal(services)

	if err := service.EnsureAgents(services); err != nil {
		slog.Warn("agent auto-registration failed", "error", err)
	}
	if err := service.EnsurePairingAccount(services); err != nil {
		slog.Warn("pairing account setup failed", "error", err)
	}

	router := api.NewRouter()
	listenAddr := fmt.Sprintf("%s:%d", cfg.Address, cfg.Port)
	listener, err := net.Listen("tcp", listenAddr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "\nGoMatrix cannot bind %s: %v\n\n", listenAddr, err)
		fmt.Fprintf(os.Stderr, "Another GoMatrix (or other process) is already using this port.\n\n")
		os.Exit(1)
	}

	server := &http.Server{
		Handler:      router,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 120 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	services.StartVaultReplySweeper(ctx)

	serverErr := make(chan error, 1)
	go func() {
		if err := server.Serve(listener); err != nil && err != http.ErrServerClosed {
			serverErr <- err
		}
	}()

	localURL := fmt.Sprintf("http://127.0.0.1:%d", cfg.Port)

	if err := waitForOwnServer(localURL, 3*time.Second); err != nil {
		slog.Error("server failed to start", "error", err)
		os.Exit(1)
	}
	slog.Info("server ready", "addr", listenAddr)

	if cfg.Tunnel.Enabled {
		go func() {
			mgr, err := tunnel.Start(ctx, cfg.Tunnel, localURL)
			if err != nil {
				slog.Error("cloudflare tunnel failed — mobile internet access unavailable", "error", err)
				return
			}
			if url := mgr.PublicURL(); url != "" {
				slog.Info("mobile connect URL ready", "url", url)
			}
		}()
	} else {
		slog.Warn("tunnel disabled — coara App can only connect on LAN; use without --no-tunnel for global access")
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	select {
	case <-stop:
		slog.Info("received shutdown signal")
	case err := <-serverErr:
		slog.Error("server error", "error", err)
	}

	slog.Info("shutting down")
	if mgr := tunnel.Global(); mgr != nil {
		mgr.Stop()
	}
	cancel()

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		slog.Error("shutdown error", "error", err)
	}
}

func waitForOwnServer(baseURL string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	client := &http.Client{Timeout: 2 * time.Second}
	statusURL := baseURL + "/api/dashboard/status"
	for time.Now().Before(deadline) {
		resp, err := client.Get(statusURL)
		if err == nil && resp.StatusCode == 200 {
			var probe struct {
				TunnelEnabled *bool `json:"tunnel_enabled"`
			}
			_ = json.NewDecoder(resp.Body).Decode(&probe)
			resp.Body.Close()
			if probe.TunnelEnabled != nil {
				return nil
			}
		}
		if resp != nil {
			resp.Body.Close()
		}
		time.Sleep(200 * time.Millisecond)
	}
	return fmt.Errorf("this GoMatrix instance did not become ready within %s", timeout)
}
