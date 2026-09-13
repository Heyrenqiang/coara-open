//go:build !windows

package logging

func hasConsole() bool {
	return true
}
