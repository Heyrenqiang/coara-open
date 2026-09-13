package tunnel

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"gomatrix/internal/config"
)

// fakeRunner is a controllable runner stand-in so watchdog tests never
// spawn a real cloudflared process.
type fakeRunner struct {
	url      string
	waitCh   chan error
	stopOnce sync.Once
}

func newFakeRunner(url string) *fakeRunner {
	return &fakeRunner{url: url, waitCh: make(chan error, 1)}
}

func (f *fakeRunner) Wait() error { return <-f.waitCh }

func (f *fakeRunner) Stop() error {
	f.stopOnce.Do(func() { close(f.waitCh) })
	return nil
}

// crash simulates an unexpected process exit (never blocks: buffer is 1).
func (f *fakeRunner) crash(err error) { f.waitCh <- err }

func (f *fakeRunner) PublicURL() string       { return f.url }
func (f *fakeRunner) SetPublicURL(url string) { f.url = url }

type launchResult struct {
	runner *fakeRunner
	err    error
}

// fakeLauncher serves queued results and records call count/timestamps.
type fakeLauncher struct {
	mu    sync.Mutex
	queue []launchResult
	times []time.Time
}

func (l *fakeLauncher) launch(ctx context.Context, opts Options) (runner, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.times = append(l.times, time.Now())
	if len(l.queue) > 0 {
		res := l.queue[0]
		l.queue = l.queue[1:]
		if res.err != nil {
			return nil, res.err
		}
		return res.runner, nil
	}
	// default: park the watchdog on a fresh runner
	return newFakeRunner(""), nil
}

func (l *fakeLauncher) callCount() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.times)
}

func (l *fakeLauncher) callTime(i int) time.Time {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.times[i]
}

func newTestManager(l *fakeLauncher) *Manager {
	m := newManager(config.TunnelConfig{Mode: "quick"}, "http://127.0.0.1:8008")
	m.launch = l.launch
	m.retryInitial = 20 * time.Millisecond
	m.retryMax = 160 * time.Millisecond
	return m
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestNextBackoff(t *testing.T) {
	max := 30 * time.Second
	cases := []struct {
		cur  time.Duration
		want time.Duration
	}{
		{1 * time.Second, 2 * time.Second},
		{2 * time.Second, 4 * time.Second},
		{4 * time.Second, 8 * time.Second},
		{16 * time.Second, max}, // 32s capped at 30s
		{max, max},
		{0, 2 * time.Second}, // zero falls back to 1s then doubles
	}
	for _, c := range cases {
		if got := nextBackoff(c.cur, max); got != c.want {
			t.Errorf("nextBackoff(%s) = %s, want %s", c.cur, got, c.want)
		}
	}
}

func TestWatchdogRestartsAfterUnexpectedExit(t *testing.T) {
	first := newFakeRunner("https://first.trycloudflare.com")
	second := newFakeRunner("https://second.trycloudflare.com")
	l := &fakeLauncher{queue: []launchResult{{runner: first}, {runner: second}}}
	m := newTestManager(l)

	if err := m.boot(context.Background()); err != nil {
		t.Fatalf("boot: %v", err)
	}
	defer m.Stop()

	if got := m.PublicURL(); got != first.url {
		t.Fatalf("PublicURL = %q, want %q", got, first.url)
	}

	first.crash(errors.New("connection reset"))

	waitFor(t, "reconnecting status", func() bool {
		snap := m.StatusSnapshot()
		return !snap.Ready && snap.State == StateReconnecting && snap.PublicURL == ""
	})
	waitFor(t, "tunnel relaunch", func() bool { return l.callCount() >= 2 })
	waitFor(t, "ready status with new URL", func() bool {
		snap := m.StatusSnapshot()
		return snap.Ready && snap.State == StateReady && snap.PublicURL == second.url && snap.Error == ""
	})
	if got := m.PublicURL(); got != second.url {
		t.Errorf("PublicURL after restart = %q, want %q", got, second.url)
	}
}

func TestWatchdogRetriesFailedLaunchesWithBackoff(t *testing.T) {
	final := newFakeRunner("https://final.trycloudflare.com")
	l := &fakeLauncher{queue: []launchResult{
		{err: errors.New("boom 1")},
		{err: errors.New("boom 2")},
		{runner: final},
	}}
	m := newTestManager(l)

	// initial launch fails — boot reports the error, watchdog keeps retrying
	if err := m.boot(context.Background()); err == nil {
		t.Fatal("expected boot error from failing launcher")
	}
	defer m.Stop()

	waitFor(t, "watchdog relaunch attempts", func() bool { return l.callCount() >= 3 })
	waitFor(t, "eventual ready status", func() bool {
		snap := m.StatusSnapshot()
		return snap.Ready && snap.State == StateReady && snap.PublicURL == final.url && snap.Error == ""
	})

	// backoff grows: call1 waits retryInitial after call0, call2 waits 2x
	if gap := l.callTime(1).Sub(l.callTime(0)); gap < m.retryInitial {
		t.Errorf("first retry gap = %s, want >= %s", gap, m.retryInitial)
	}
	if gap := l.callTime(2).Sub(l.callTime(1)); gap < 2*m.retryInitial {
		t.Errorf("second retry gap = %s, want >= %s (exponential backoff)", gap, 2*m.retryInitial)
	}
}

func TestStopPreventsRestart(t *testing.T) {
	first := newFakeRunner("https://first.trycloudflare.com")
	l := &fakeLauncher{queue: []launchResult{{runner: first}}}
	m := newTestManager(l)

	if err := m.boot(context.Background()); err != nil {
		t.Fatalf("boot: %v", err)
	}

	m.Stop()
	m.Stop() // idempotent

	snap := m.StatusSnapshot()
	if snap.State != StateStopped || snap.Ready {
		t.Errorf("status after Stop = %+v, want stopped/not-ready", snap)
	}

	calls := l.callCount()
	time.Sleep(150 * time.Millisecond) // several backoff cycles
	if got := l.callCount(); got != calls {
		t.Errorf("launcher called %d extra time(s) after Stop", got-calls)
	}
}

func TestWatchdogNamedModeKeepsConfiguredURL(t *testing.T) {
	first := newFakeRunner("")
	second := newFakeRunner("")
	l := &fakeLauncher{queue: []launchResult{{runner: first}, {runner: second}}}
	m := newTestManager(l)
	m.cfg.PublicURL = "https://matrix.example.com/"
	m.mode = ModeNamed
	m.opts.Mode = ModeNamed

	if err := m.boot(context.Background()); err != nil {
		t.Fatalf("boot: %v", err)
	}
	defer m.Stop()

	if got := m.PublicURL(); got != "https://matrix.example.com" {
		t.Fatalf("PublicURL = %q, want configured URL", got)
	}

	first.crash(errors.New("died"))

	waitFor(t, "restart with configured URL", func() bool {
		return l.callCount() >= 2 && m.PublicURL() == "https://matrix.example.com"
	})
	if second.url != "https://matrix.example.com" {
		t.Errorf("second runner URL = %q, want configured URL reapplied", second.url)
	}
}
