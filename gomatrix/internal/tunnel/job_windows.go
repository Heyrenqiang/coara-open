//go:build windows

package tunnel

import (
	"fmt"
	"os/exec"
	"sync"
	"unsafe"

	"golang.org/x/sys/windows"
)

// cloudflared instances are assigned to a single Job Object with
// KILL_ON_JOB_CLOSE: when the last job handle dies with the gomatrix
// process (graceful exit, crash, or Task Manager kill alike), Windows
// terminates every assigned process, so cloudflared can never be orphaned
var (
	jobOnce   sync.Once
	jobHandle windows.Handle
	jobErr    error
)

func ensureJobObject() (windows.Handle, error) {
	jobOnce.Do(func() {
		handle, err := windows.CreateJobObject(nil, nil)
		if err != nil {
			jobErr = fmt.Errorf("CreateJobObject: %w", err)
			return
		}
		info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
		info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
		if _, err := windows.SetInformationJobObject(
			handle,
			windows.JobObjectExtendedLimitInformation,
			uintptr(unsafe.Pointer(&info)),
			uint32(unsafe.Sizeof(info)),
		); err != nil {
			_ = windows.CloseHandle(handle)
			jobErr = fmt.Errorf("SetInformationJobObject: %w", err)
			return
		}
		// The handle intentionally stays open for the whole process lifetime
		jobHandle = handle
	})
	return jobHandle, jobErr
}

// bindToParentLifetime assigns a started subprocess to the Job Object so its
// lifetime is tied to the gomatrix process.
func bindToParentLifetime(cmd *exec.Cmd) error {
	if cmd == nil || cmd.Process == nil {
		return nil
	}
	job, err := ensureJobObject()
	if err != nil {
		return err
	}
	// os.Process does not export its handle, so reopen by PID with the
	// access rights AssignProcessToJobObject requires
	handle, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(cmd.Process.Pid))
	if err != nil {
		return fmt.Errorf("OpenProcess: %w", err)
	}
	defer func() { _ = windows.CloseHandle(handle) }()
	if err := windows.AssignProcessToJobObject(job, handle); err != nil {
		return fmt.Errorf("AssignProcessToJobObject: %w", err)
	}
	return nil
}
