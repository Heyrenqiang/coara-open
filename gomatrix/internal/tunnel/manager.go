package tunnel

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"gomatrix/internal/config"
)

// Status describes the current tunnel state for dashboard/API consumers.
type Status struct {
	Enabled   bool   `json:"enabled"`
	Ready     bool   `json:"ready"`
	Mode      string `json:"mode"`
	State     string `json:"state"`
	PublicURL string `json:"public_url"`
	Error     string `json:"error,omitempty"`
}

// Tunnel lifecycle states reported through Status.State.
const (
	StateStarting     = "starting"
	StateReady        = "ready"
	StateReconnecting = "reconnecting"
	StateError        = "error"
	StateStopped      = "stopped"
)

// Watchdog backoff bounds for tunnel restarts: 1s, 2s, 4s, ... capped at 30s
const (
	defaultRetryInitial = 1 * time.Second
	defaultRetryMax     = 30 * time.Second
)

// heartbeatInterval is the liveness-trail cadence: one INFO line per tick so a
// silent log for >interval itself signals trouble (phone issues become
// attributable to tunnel vs gomatrix at a glance).
const heartbeatInterval = 5 * time.Minute

// runner is the subprocess behavior the watchdog supervises
// (*Tunnel implements it; tests inject fakes).
type runner interface {
	Wait() error
	Stop() error
	PublicURL() string
	SetPublicURL(string)
}

var (
	globalMu sync.RWMutex
	global   *Manager
)

// Manager owns the active cloudflared subprocess and the watchdog that
// restarts it after unexpected exits.
type Manager struct {
	cfg      config.TunnelConfig
	localURL string
	mode     Mode
	opts     Options

	// launch is replaceable in tests so the watchdog can be exercised
	// without a real cloudflared binary
	launch       func(context.Context, Options) (runner, error)
	retryInitial time.Duration
	retryMax     time.Duration

	ctx    context.Context
	cancel context.CancelFunc

	mu       sync.RWMutex
	tunnel   runner
	status   Status
	wg       sync.WaitGroup
	stopOnce sync.Once

	startedAt time.Time
	restarts  int
}

// SetGlobal stores the active tunnel manager for dashboard handlers.
func SetGlobal(m *Manager) {
	globalMu.Lock()
	global = m
	globalMu.Unlock()
}

// Global returns the active tunnel manager, if any.
func Global() *Manager {
	globalMu.RLock()
	defer globalMu.RUnlock()
	return global
}

// Start launches cloudflared when enabled in config and starts the watchdog
// that restarts it after unexpected exits.
func Start(parentCtx context.Context, cfg config.TunnelConfig, localURL string) (*Manager, error) {
	m := newManager(cfg, localURL)

	if !IsAvailable(cfg.BinaryPath) {
		err := fmt.Errorf("cloudflared not found on PATH — install from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/")
		m.failStatus(StateError, err)
		SetGlobal(m)
		return m, err
	}

	err := m.boot(parentCtx)
	SetGlobal(m)
	return m, err
}

// newManager builds a Manager with production defaults; tests override
// launch/backoff fields before calling boot.
func newManager(cfg config.TunnelConfig, localURL string) *Manager {
	mode := ModeQuick
	if strings.EqualFold(cfg.Mode, "named") {
		mode = ModeNamed
	}
	return &Manager{
		cfg:      cfg,
		localURL: localURL,
		mode:     mode,
		opts: Options{
			Mode:       mode,
			LocalURL:   localURL,
			Token:      cfg.Token,
			BinaryPath: cfg.BinaryPath,
		},
		launch:       defaultLaunch,
		retryInitial: defaultRetryInitial,
		retryMax:     defaultRetryMax,
		status: Status{
			Enabled: true,
			Mode:    cfg.Mode,
			State:   StateStarting,
		},
	}
}

func defaultLaunch(ctx context.Context, opts Options) (runner, error) {
	return Launch(ctx, opts)
}

// boot performs the initial launch and starts the watchdog goroutine.
// An initial launch failure is reported to the caller, but the watchdog
// keeps retrying in the background so transient errors self-heal.
func (m *Manager) boot(parentCtx context.Context) error {
	m.ctx, m.cancel = context.WithCancel(parentCtx)
	m.startedAt = time.Now()

	t, err := m.launch(m.ctx, m.opts)
	if err != nil {
		m.failStatus(StateReconnecting, err)
		m.startWatchdog()
		m.startHeartbeat()
		return err
	}

	m.adopt(t)
	m.startWatchdog()
	m.startHeartbeat()

	if snap := m.StatusSnapshot(); snap.Ready {
		slog.Info("mobile tunnel ready", "url", snap.PublicURL, "mode", m.mode)
	}
	return nil
}

// startHeartbeat logs a one-line liveness/state summary every heartbeatInterval.
func (m *Manager) startHeartbeat() {
	m.wg.Add(1)
	go func() {
		defer m.wg.Done()
		ticker := time.NewTicker(heartbeatInterval)
		defer ticker.Stop()
		for {
			select {
			case <-m.ctx.Done():
				return
			case <-ticker.C:
				snap := m.StatusSnapshot()
				m.mu.RLock()
				restarts := m.restarts
				m.mu.RUnlock()
				slog.Info(
					"tunnel heartbeat",
					"state", snap.State,
					"url", snap.PublicURL,
					"restarts", restarts,
					"uptime", time.Since(m.startedAt).Round(time.Second).String(),
				)
			}
		}
	}()
}

// adopt installs a freshly launched runner and marks the tunnel ready.
func (m *Manager) adopt(t runner) {
	if m.mode == ModeNamed && m.cfg.PublicURL != "" {
		t.SetPublicURL(strings.TrimRight(m.cfg.PublicURL, "/"))
	}
	url := t.PublicURL()

	m.mu.Lock()
	m.tunnel = t
	m.status.Ready = url != ""
	m.status.PublicURL = url
	m.status.Error = ""
	if url != "" {
		m.status.State = StateReady
	} else {
		m.status.State = StateStarting
	}
	m.mu.Unlock()
}

// failStatus records a non-ready state under lock.
func (m *Manager) failStatus(state string, err error) {
	m.mu.Lock()
	m.status.Ready = false
	m.status.State = state
	m.status.PublicURL = ""
	m.status.Error = err.Error()
	m.mu.Unlock()
}

// startWatchdog launches the supervisor goroutine that restarts cloudflared
// after unexpected exits with exponential backoff.
func (m *Manager) startWatchdog() {
	m.wg.Add(1)
	go m.watchdog()
}

func (m *Manager) watchdog() {
	defer m.wg.Done()
	backoff := m.retryInitial
	failures := 0

	for {
		if t := m.currentRunner(); t != nil {
			waitErr := t.Wait()
			if m.ctx.Err() != nil {
				return // deliberate Stop
			}
			m.mu.Lock()
			if m.tunnel == t {
				m.tunnel = nil
			}
			m.mu.Unlock()
			slog.Warn("cloudflared exited unexpectedly — restarting tunnel", "error", waitErr)
			exitErr := waitErr
			if exitErr == nil {
				exitErr = fmt.Errorf("process terminated")
			}
			m.failStatus(StateReconnecting, fmt.Errorf("cloudflared exited: %w", exitErr))
		}

		select {
		case <-m.ctx.Done():
			return
		case <-time.After(backoff):
		}

		t, err := m.launch(m.ctx, m.opts)
		if m.ctx.Err() != nil {
			if err == nil && t != nil {
				_ = t.Stop()
			}
			return
		}
		if err != nil {
			failures++
			slog.Error("cloudflared restart failed", "error", err, "consecutive_failures", failures, "retry_in", backoff)
			m.failStatus(StateReconnecting, err)
			backoff = nextBackoff(backoff, m.retryMax)
			continue
		}

		failures = 0
		backoff = m.retryInitial
		m.mu.Lock()
		m.restarts++
		m.mu.Unlock()
		m.adopt(t)
		if snap := m.StatusSnapshot(); snap.Ready {
			slog.Info("cloudflared restarted", "url", snap.PublicURL, "mode", m.mode)
		} else {
			slog.Info("cloudflared restarted — waiting for tunnel URL", "mode", m.mode)
		}
	}
}

// nextBackoff doubles the wait between restart attempts up to max.
func nextBackoff(cur, max time.Duration) time.Duration {
	if cur <= 0 {
		cur = time.Second
	}
	next := cur * 2
	if max > 0 && next > max {
		return max
	}
	return next
}

func (m *Manager) currentRunner() runner {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.tunnel
}

// StatusSnapshot returns a copy of the current tunnel status.
func (m *Manager) StatusSnapshot() Status {
	if m == nil {
		return Status{}
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.status
}

// PublicURL returns the tunnel public URL when ready.
func (m *Manager) PublicURL() string {
	if m == nil {
		return ""
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	if !m.status.Ready {
		return ""
	}
	return m.status.PublicURL
}

// Stop terminates the cloudflared subprocess and shuts down the watchdog
// without triggering a restart.
func (m *Manager) Stop() {
	if m == nil {
		return
	}
	m.stopOnce.Do(func() {
		if m.cancel != nil {
			m.cancel()
		}
		m.mu.Lock()
		t := m.tunnel
		m.tunnel = nil
		m.mu.Unlock()
		if t != nil {
			_ = t.Stop()
		}
		m.wg.Wait()
		m.mu.Lock()
		m.status.Ready = false
		m.status.State = StateStopped
		m.mu.Unlock()
	})
}
