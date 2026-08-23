package runtime

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRedactSecrets(t *testing.T) {
	tests := []struct {
		name   string
		input  string
		secret string
	}{
		{"current enrollment token", "enroll zpa_enr_abcdefghijklmnopqrstuvwxyz012345 next", "abcdefghijklmnopqrstuvwxyz012345"},
		{"legacy enrollment token", "enroll zp_enroll_legacy-token next", "legacy-token"},
		{"bearer", "Authorization: Bearer abc.def.ghi next", "abc.def.ghi"},
		{"token property", "token=supersecret next", "supersecret"},
		{"credential property", `credential: "very-secret" next`, "very-secret"},
		{"enrollment token property", "enrollment_token: another-secret next", "another-secret"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := Redact(tt.input)
			if strings.Contains(got, tt.secret) {
				t.Fatalf("secret remained in redacted output: %q", got)
			}
			if !strings.Contains(got, "REDACTED") {
				t.Fatalf("redaction marker missing from %q", got)
			}
		})
	}
}

func TestLoggerRedactsBeforeWriting(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.log")
	logger, err := NewLogger(path, false)
	if err != nil {
		t.Fatal(err)
	}
	logger.Printf("enrollment token=%s", "zpa_enr_file_secret")
	if err := logger.Close(); err != nil {
		t.Fatal(err)
	}

	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	text := string(b)
	if strings.Contains(text, "file_secret") {
		t.Fatalf("logger wrote a secret: %q", text)
	}
	if !strings.Contains(text, "token=REDACTED") {
		t.Fatalf("logger did not write the redaction marker: %q", text)
	}
}
