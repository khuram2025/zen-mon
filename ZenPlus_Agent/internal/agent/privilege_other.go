//go:build !windows

package agent

func canManageApplicationInstrumentation() bool { return false }
