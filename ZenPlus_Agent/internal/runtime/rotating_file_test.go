package runtime

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLogRotationIsBoundedAndPreservesRecentBackups(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "telemetry-gateway.log")
	for name, contents := range map[string]string{
		path:        "current-log",
		path + ".1": "previous-log",
		path + ".2": "oldest-log",
	} {
		if err := os.WriteFile(name, []byte(contents), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := rotateFile(path, 4, 2); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("active log still exists after rotation: %v", err)
	}
	for name, want := range map[string]string{
		path + ".1": "current-log",
		path + ".2": "previous-log",
	} {
		got, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != want {
			t.Fatalf("%s = %q, want %q", name, got, want)
		}
	}

	if err := os.WriteFile(path, []byte("ok"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := rotateFile(path, 4, 2); err != nil {
		t.Fatal(err)
	}
	if got, err := os.ReadFile(path); err != nil || string(got) != "ok" {
		t.Fatalf("small active log was rotated: %q, %v", got, err)
	}

	runtimePath := filepath.Join(dir, "runtime.log")
	writer, err := OpenRotatingFile(runtimePath, 4, 2)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := writer.Write([]byte("abcdefghij")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{
		runtimePath:        "ij",
		runtimePath + ".1": "efgh",
		runtimePath + ".2": "abcd",
	} {
		got, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != want {
			t.Fatalf("runtime rotation %s = %q, want %q", name, got, want)
		}
	}
}
