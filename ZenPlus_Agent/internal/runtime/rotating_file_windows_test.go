//go:build windows

package runtime

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// On Windows a file another process has open cannot be renamed. Rotation
// must then keep logging to the current file and retry later, instead of
// leaving the log closed.
func TestRotationKeepsLoggingWhenTheLogIsHeldOpen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.log")
	w, err := OpenRotatingFile(path, 8, 2)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	if _, err := w.Write([]byte("12345678")); err != nil {
		t.Fatal(err)
	}
	reader, err := os.Open(path) // like the dashboard tailing the log
	if err != nil {
		t.Fatal(err)
	}
	if _, err := w.Write([]byte("abc")); err != nil {
		t.Fatalf("write failed while the log was held open: %v", err)
	}
	reader.Close()
	if got, _ := os.ReadFile(path); string(got) != "12345678abc" {
		t.Fatalf("log = %q, want the line appended to the current file", got)
	}

	w.mu.Lock()
	w.retryRotateAt = time.Now().Add(-time.Second)
	w.mu.Unlock()
	if _, err := w.Write([]byte("xyz")); err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(path + ".1"); string(got) != "12345678abc" {
		t.Fatalf("backup = %q, want the previous log after the retried rotation", got)
	}
	if got, _ := os.ReadFile(path); string(got) != "xyz" {
		t.Fatalf("active log = %q after rotation", got)
	}
}
