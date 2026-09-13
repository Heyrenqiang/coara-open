//go:build windows

package tunnel

import (
	"os/exec"
	"syscall"
)

const createNoWindow = 0x08000000

func applyHiddenSubprocess(cmd *exec.Cmd) {
	if cmd == nil {
		return
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: createNoWindow,
	}
}
