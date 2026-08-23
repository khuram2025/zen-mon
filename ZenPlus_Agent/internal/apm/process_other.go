//go:build !windows

package apm

import "os/exec"

func setHiddenProcess(cmd *exec.Cmd) {}

func attachProcessLifecycle(cmd *exec.Cmd) (func(), error) {
	return func() {}, nil
}
