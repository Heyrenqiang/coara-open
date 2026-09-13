package logging

import (
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
)

// maxLogBytes triggers startup rotation: an oversized log from the previous run
// is renamed aside (single backup) before appending continues.
const maxLogBytes = 10 << 20

// Setup configures slog. On Windows without a console, logs go to a file only.
func Setup(level slog.Level, logFile string) (io.Closer, error) {
	writers := make([]io.Writer, 0, 2)

	if logFile == "" {
		logFile = defaultLogPath()
	}
	if err := os.MkdirAll(filepath.Dir(logFile), 0o755); err != nil {
		return nil, fmt.Errorf("create log dir: %w", err)
	}
	rotateLogFile(logFile)
	file, err := os.OpenFile(logFile, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open log file: %w", err)
	}
	writers = append(writers, file)

	if runtime.GOOS != "windows" || hasConsole() {
		writers = append(writers, os.Stdout)
	}

	var out io.Writer
	switch len(writers) {
	case 1:
		out = writers[0]
	default:
		out = io.MultiWriter(writers...)
	}

	slog.SetDefault(slog.New(slog.NewTextHandler(out, &slog.HandlerOptions{Level: level})))
	slog.Info("logging ready", "file", logFile, "console", len(writers) > 1)
	return file, nil
}

func defaultLogPath() string {
	if runtime.GOOS == "windows" {
		if base := os.Getenv("LOCALAPPDATA"); base != "" {
			return filepath.Join(base, "GoMatrix", "gomatrix.log")
		}
	}
	if home, err := os.UserHomeDir(); err == nil && home != "" {
		return filepath.Join(home, ".gomatrix", "gomatrix.log")
	}
	return "./gomatrix.log"
}

// rotateLogFile keeps one backup: gomatrix.log → gomatrix.log.1 when oversized.
func rotateLogFile(logFile string) {
	info, err := os.Stat(logFile)
	if err != nil || info.Size() < maxLogBytes {
		return
	}
	backup := logFile + ".1"
	_ = os.Remove(backup)
	if err := os.Rename(logFile, backup); err != nil {
		// Rotation is best-effort; never block startup on it.
		fmt.Fprintf(os.Stderr, "log rotation failed: %v\n", err)
	}
}
