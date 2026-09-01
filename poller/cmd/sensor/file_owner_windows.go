//go:build windows

package main

func preserveFileOwnership(_, _ string) error {
	return nil
}
