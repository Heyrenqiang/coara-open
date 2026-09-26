// Package tunnel manages a Cloudflare tunnel (cloudflared) subprocess to expose
// the local GoMatrix homeserver to the public internet, enabling coara App to
// connect from anywhere — not just LAN.
package tunnel

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"log/slog"
	"os/exec"
	"regexp"
	"strings"
	"sync"
	"time"
)

// Mode controls which kind of Cloudflare tunnel to start.
type Mode string

const (
	ModeQuick Mode = "quick"
	ModeNamed Mode = "named"
)

// Options configures tunnel startup.
type Options struct {
	Mode       Mode
	LocalURL   string
	Token      string
	BinaryPath string
	Timeout    time.Duration
}

// Tunnel is a live cloudflared subprocess.
type Tunnel struct {
	cmd        *exec.Cmd
	cancel     context.CancelFunc
	publicURL  string
	wg         sync.WaitGroup
	stdoutPipe io.ReadCloser
	stderrPipe io.ReadCloser
}

var tunnelURLRegex = regexp.MustCompile(`https://[a-zA-Z0-9-]+\.trycloudflare\.com`)

// Launch starts a cloudflared subprocess and waits for the public URL.
func Launch(parentCtx context.Context, opts Options) (*Tunnel, error) {
	if opts.Mode == "" {
		opts.Mode = ModeQuick
	}
	if opts.Timeout <= 0 {
		opts.Timeout = 30 * time.Second
	}

	binary := opts.BinaryPath
	if binary == "" {
		binary = "cloudflared"
	}

	var args []string
	switch opts.Mode {
	case ModeQuick:
		if opts.LocalURL == "" {
			return nil, fmt.Errorf("local_url is required for quick tunnel mode")
		}
		args = []string{"tunnel", "--no-autoupdate", "--url", opts.LocalURL}
	case ModeNamed:
		if opts.Token == "" {
			return nil, fmt.Errorf("token is required for named tunnel mode")
		}
		args = []string{"tunnel", "--no-autoupdate", "run", "--token", opts.Token}
	default:
		return nil, fmt.Errorf("unknown tunnel mode: %s", opts.Mode)
	}

	ctx, cancel := context.WithCancel(parentCtx)
	cmd := exec.CommandContext(ctx, binary, args...)
	cmd.Env = cmd.Environ()
	applyHiddenSubprocess(cmd)

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		cancel()
		return nil, fmt.Errorf("stdout pipe: %w", err)
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		cancel()
		return nil, fmt.Errorf("stderr pipe: %w", err)
	}

	if err := cmd.Start(); err != nil {
		cancel()
		return nil, fmt.Errorf("starting cloudflared: %w (install from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)", err)
	}

	if err := bindToParentLifetime(cmd); err != nil {
		slog.Warn("failed to bind cloudflared to gomatrix process lifetime", "error", err)
	}

	slog.Info("cloudflared started", "mode", opts.Mode, "pid", cmd.Process.Pid)

	t := &Tunnel{
		cmd:        cmd,
		cancel:     cancel,
		stdoutPipe: stdout,
		stderrPipe: stderr,
	}

	if opts.Mode == ModeNamed {
		urlCh := make(chan string, 1)
		t.wg.Add(1)
		go func() {
			defer t.wg.Done()
			t.scanOutput(urlCh)
		}()

		select {
		case url := <-urlCh:
			t.publicURL = url
		case <-time.After(opts.Timeout):
			slog.Warn("tunnel started but URL not detected within timeout", "timeout", opts.Timeout)
		case <-ctx.Done():
			return t, ctx.Err()
		}
		return t, nil
	}

	urlCh := make(chan string, 1)
	errCh := make(chan error, 1)

	t.wg.Add(1)
	go func() {
		defer t.wg.Done()
		t.scanOutput(urlCh, errCh)
	}()

	select {
	case url := <-urlCh:
		t.publicURL = url
		slog.Info("cloudflare tunnel URL acquired", "url", url)
	case err := <-errCh:
		_ = t.Stop()
		return nil, fmt.Errorf("cloudflared exited: %w", err)
	case <-time.After(opts.Timeout):
		_ = t.Stop()
		return nil, fmt.Errorf("timed out waiting for tunnel URL after %s — check that cloudflared is working", opts.Timeout)
	case <-ctx.Done():
		_ = t.Stop()
		return nil, ctx.Err()
	}

	return t, nil
}

func (t *Tunnel) scanOutput(urlCh chan<- string, errCh ...chan error) {
	urlSent := false

	go func() {
		scannerStdout := bufio.NewScanner(t.stdoutPipe)
		scannerStdout.Buffer(make([]byte, 0, 64*1024), 256*1024)
		for scannerStdout.Scan() {
			line := scannerStdout.Text()
			logCloudflaredLine(line)
			if !urlSent {
				if url := extractTunnelURL(line); url != "" {
					urlSent = true
					urlCh <- url
				}
			}
		}
	}()

	scanner := bufio.NewScanner(t.stderrPipe)
	scanner.Buffer(make([]byte, 0, 64*1024), 256*1024)
	for scanner.Scan() {
		line := scanner.Text()
		logCloudflaredLine(line)

		if !urlSent {
			if url := extractTunnelURL(line); url != "" {
				urlSent = true
				urlCh <- url
			}
		}

		lower := strings.ToLower(line)
		if strings.Contains(lower, "error") && strings.Contains(lower, "failed") {
			if !urlSent && len(errCh) > 0 {
				errCh[0] <- fmt.Errorf("cloudflared: %s", line)
			}
		}
	}

	if !urlSent && len(errCh) > 0 {
		errCh[0] <- fmt.Errorf("cloudflared process exited before URL was available")
	}
}

func extractTunnelURL(line string) string {
	return tunnelURLRegex.FindString(line)
}

// logCloudflaredLine maps cloudflared output to visible slog levels — without
// this the tunnel can flap for days while the gomatrix log stays silent
// (everything used to land in Debug, filtered at the default info level).
func logCloudflaredLine(line string) {
	lower := strings.ToLower(line)
	switch {
	case strings.Contains(lower, "error") || strings.Contains(lower, "failed") || strings.Contains(lower, "warn"):
		slog.Warn("cloudflared", "line", line)
	case strings.Contains(lower, "registered tunnel connection"),
		strings.Contains(lower, "connected"),
		strings.Contains(lower, "connection lost"),
		strings.Contains(lower, "disconnected"),
		strings.Contains(lower, "retrying"):
		slog.Info("cloudflared", "line", line)
	default:
		slog.Debug("cloudflared", "line", line)
	}
}

func (t *Tunnel) PublicURL() string {
	if t == nil {
		return ""
	}
	return t.publicURL
}

func (t *Tunnel) SetPublicURL(url string) {
	if t != nil {
		t.publicURL = url
	}
}

// Wait blocks until the cloudflared process exits and reaps it.
// It must be called exactly once per Tunnel (the watchdog owns this call).
func (t *Tunnel) Wait() error {
	if t == nil || t.cmd == nil {
		return nil
	}
	return t.cmd.Wait()
}

func (t *Tunnel) Stop() error {
	if t == nil || t.cmd == nil || t.cmd.Process == nil {
		return nil
	}
	slog.Info("stopping cloudflared tunnel")
	t.cancel()

	done := make(chan struct{})
	go func() {
		t.wg.Wait()
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(5 * time.Second):
		_ = t.cmd.Process.Kill()
	}

	return nil
}

func IsAvailable(binaryPath string) bool {
	binary := binaryPath
	if binary == "" {
		binary = "cloudflared"
	}
	cmd := exec.Command(binary, "version")
	applyHiddenSubprocess(cmd)
	return cmd.Run() == nil
}
