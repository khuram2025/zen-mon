//go:build windows

package apm

import (
	"reflect"
	"testing"
)

func TestServiceEnvironmentRoundTripPreservesUnrelatedValues(t *testing.T) {
	original := []string{"KEEP=original", "NODE_OPTIONS=--max-old-space-size=512", "malformed-entry"}
	managed := runtimeServiceEnvironment("node", `C:\Program Files\ZenPlus\apm\instrumentation\node`, "orders-api", "prod", original)
	previous := captureEnvironmentValues(original, managed)
	applied := setEnvironmentValues(original, managed)
	if value, found := findEnvironmentValue(applied, "NODE_OPTIONS"); !found || value == "--max-old-space-size=512" {
		t.Fatalf("Node preload was not appended: %q", value)
	}
	restored := restoreEnvironmentValues(applied, previous)
	if !reflect.DeepEqual(restored, original) {
		t.Fatalf("service environment was not restored exactly\n got: %#v\nwant: %#v", restored, original)
	}
}

func TestProtectedServicesCannotBeInstrumented(t *testing.T) {
	for _, name := range []string{"RpcSs", "EventLog", "ZenPlusAgent"} {
		if err := validateManagedWindowsService(name); err == nil {
			t.Fatalf("expected %q to be protected", name)
		}
	}
	if err := validateManagedWindowsService("ContosoOrders"); err != nil {
		t.Fatalf("ordinary application service rejected: %v", err)
	}
}
