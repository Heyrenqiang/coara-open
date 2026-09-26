//go:build !windows

package tunnel

import "os/exec"

func applyHiddenSubprocess(cmd *exec.Cmd) {}
