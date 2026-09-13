//go:build !windows

package tunnel

import "os/exec"

// bindToParentLifetime is a no-op outside Windows (Job Objects are
// Windows-only; POSIX platforms rely on context cancellation on shutdown).
func bindToParentLifetime(cmd *exec.Cmd) error { return nil }
