//go:build windows

package apm

import (
	"reflect"
	"strings"
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

func TestRuntimeSwitchRestoresPriorManagedEnvironment(t *testing.T) {
	original := []string{"KEEP=original", "JAVA_TOOL_OPTIONS=-Xmx512m"}
	javaManaged := runtimeServiceEnvironment("java", `C:\ZenPlus\java\opentelemetry-javaagent.jar`, "orders", "prod", original)
	javaPrevious := captureEnvironmentValues(original, javaManaged)
	javaApplied := setEnvironmentValues(original, javaManaged)
	baseline := restoreEnvironmentValues(javaApplied, javaPrevious)
	nodeManaged := runtimeServiceEnvironment("node", `C:\ZenPlus\node`, "orders", "prod", baseline)
	nodeApplied := setEnvironmentValues(baseline, nodeManaged)

	javaOptions, _ := findEnvironmentValue(nodeApplied, "JAVA_TOOL_OPTIONS")
	if strings.Contains(strings.ToLower(javaOptions), "javaagent") {
		t.Fatalf("Java instrumentation survived a switch to Node: %q", javaOptions)
	}
	if javaOptions != "-Xmx512m" {
		t.Fatalf("original JAVA_TOOL_OPTIONS not restored: %q", javaOptions)
	}
	nodeOptions, found := findEnvironmentValue(nodeApplied, "NODE_OPTIONS")
	if !found || !strings.Contains(strings.ToLower(nodeOptions), "bootstrap.js") {
		t.Fatalf("Node instrumentation not applied: %q", nodeOptions)
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
