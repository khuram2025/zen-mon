//go:build windows

package apm

import (
	"strings"
	"testing"
)

func TestDecodeIISConfigurationResultIgnoresPowerShellObjectOutput(t *testing.T) {
	output := []byte("Attributes : Microsoft.Web.Administration.ConfigurationAttributeCollection\r\n" +
		iisConfigurationResultPrefix + `{"previous":{"OTEL_SERVICE_NAME":null},"restarted":true,"restart_error":""}` + "\r\n")

	result, err := decodeIISConfigurationResult(output)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Restarted || result.RestartError != "" {
		t.Fatalf("unexpected IIS result: %#v", result)
	}
	if previous, ok := result.Previous["OTEL_SERVICE_NAME"]; !ok || previous != nil {
		t.Fatalf("previous environment was not decoded: %#v", result.Previous)
	}
}

func TestDecodeIISConfigurationResultAcceptsLegacyJSON(t *testing.T) {
	result, err := decodeIISConfigurationResult([]byte(`{"previous":{},"restarted":false,"restart_error":""}`))
	if err != nil {
		t.Fatal(err)
	}
	if result.Restarted || result.RestartError != "" {
		t.Fatalf("unexpected legacy IIS result: %#v", result)
	}
}

func TestIISConfigurationScriptEmitsOnlyMarkedResult(t *testing.T) {
	if count := strings.Count(iisConfigurationScript, "[void]$environmentVariables.Add($entry)"); count != 2 {
		t.Fatalf("IIS script suppresses %d collection Add result(s), want 2", count)
	}
	if !strings.Contains(iisConfigurationScript, "Write-Output ('__ZENPLUS_IIS_RESULT__' + $resultJson)") {
		t.Fatal("IIS script does not emit a marked JSON result")
	}
}
