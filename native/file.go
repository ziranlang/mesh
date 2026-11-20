//go:build mesh_embedded

package native

import (
	"os"
	"syscall"
)

// LockFile adapts Go's uintptr file descriptor to the integer flock API.
func LockFile(file *os.File, how int) error {
	return syscall.Flock(int(file.Fd()), how)
}
