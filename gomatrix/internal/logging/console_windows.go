//go:build windows

package logging

import "syscall"

func hasConsole() bool {
	kernel32 := syscall.NewLazyDLL("kernel32.dll")
	getConsoleWindow := kernel32.NewProc("GetConsoleWindow")
	handle, _, _ := getConsoleWindow.Call()
	return handle != 0
}
