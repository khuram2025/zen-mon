package secrets

import (
	"strings"
	"testing"
)

func TestMaskKeepsSecretIdentifiable(t *testing.T) {
	got := Mask("zpa_key_AbC123dEf456GhI789jKl012MnO345pQr678")
	if got != "zpa_key_******pQr678" {
		t.Fatalf("unexpected mask %q", got)
	}
}

func TestMetadataLabelIncludesShortHash(t *testing.T) {
	meta := NewMetadata([]byte("zpa_key_AbC123dEf456GhI789jKl012MnO345pQr678"))
	label := meta.Label()
	if label == "" {
		t.Fatal("empty label")
	}
	if meta.SHA256 == "" || len(meta.SHA256) != 64 {
		t.Fatalf("unexpected sha256 %q", meta.SHA256)
	}
	if want := "sha256 " + meta.SHA256[:12]; !strings.Contains(label, want) {
		t.Fatalf("label %q missing %q", label, want)
	}
}
