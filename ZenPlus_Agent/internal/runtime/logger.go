package runtime

import (
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"
)

type Logger struct {
	*log.Logger
	file *os.File
}

func NewLogger(path string, mirrorStdout bool) (*Logger, error) {
	if path == "" {
		path = "agent.log"
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	var w io.Writer = f
	if mirrorStdout {
		w = io.MultiWriter(os.Stdout, f)
	}
	return &Logger{Logger: log.New(w, "", log.LstdFlags|log.LUTC), file: f}, nil
}

func (l *Logger) Close() error {
	if l == nil || l.file == nil {
		return nil
	}
	return l.file.Close()
}

func Redact(s string) string {
	for _, marker := range []string{"zp_enroll_", "Bearer ", "credential=", "token="} {
		if idx := strings.Index(s, marker); idx >= 0 {
			return s[:idx+len(marker)] + "REDACTED"
		}
	}
	return s
}
