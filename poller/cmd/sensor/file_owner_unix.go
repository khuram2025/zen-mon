//go:build linux || darwin || freebsd || openbsd || netbsd

package main

import (
	"fmt"
	"os"
	"syscall"
)

func preserveFileOwnership(source, destination string) error {
	info, err := os.Stat(source)
	if err != nil {
		return err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return fmt.Errorf("read ownership for %s", source)
	}
	return os.Chown(destination, int(stat.Uid), int(stat.Gid))
}
