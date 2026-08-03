package runtime

import (
	"io"
	"log"
	"os"
	"path/filepath"
	"regexp"
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
	return &Logger{Logger: log.New(redactingWriter{Writer: w}, "", log.LstdFlags|log.LUTC), file: f}, nil
}

func (l *Logger) Close() error {
	if l == nil || l.file == nil {
		return nil
	}
	return l.file.Close()
}

func Redact(s string) string {
	for _, rule := range redactionRules {
		s = rule.pattern.ReplaceAllString(s, rule.replacement)
	}
	return s
}

type redactingWriter struct {
	io.Writer
}

func (w redactingWriter) Write(p []byte) (int, error) {
	redacted := []byte(Redact(string(p)))
	_, err := w.Writer.Write(redacted)
	if err != nil {
		return 0, err
	}
	// log.Logger expects a successful writer to report the length of its input,
	// even when redaction changes the output length.
	return len(p), nil
}

var redactionRules = []struct {
	pattern     *regexp.Regexp
	replacement string
}{
	{regexp.MustCompile(`(?i)\b(zpa_enr_|zp_enroll_)[a-z0-9._~-]+`), `${1}REDACTED`},
	{regexp.MustCompile(`(?i)(\bbearer\s+)[^\s,;"']+`), `${1}REDACTED`},
	{regexp.MustCompile(`(?i)(\b(?:enrollment_token|token|credential)\s*[:=]\s*["']?)[^\s,;"'&]+`), `${1}REDACTED`},
}
