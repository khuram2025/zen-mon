//go:build windows

package main

import (
	"testing"
)

func TestHardenMachineDataTreeRejectsRelativeRoot(t *testing.T) {
	if err := hardenMachineDataTree(`relative\path`, serviceName); err == nil {
		t.Fatal("relative machine data path was accepted")
	}
}
