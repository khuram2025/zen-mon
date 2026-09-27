package runtime

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"time"
)

// RotatingFile is an append-only log file that keeps at most maxBytes in the
// active file and a fixed number of numbered backups (path.1 is the newest).
// It is shared by the agent log and the managed APM gateway log.
type RotatingFile struct {
	mu       sync.Mutex
	path     string
	maxBytes int64
	backups  int
	file     *os.File
	size     int64
	// retryRotateAt defers rotation after a failed rename.
	retryRotateAt time.Time
}

func OpenRotatingFile(path string, maxBytes int64, backups int) (*RotatingFile, error) {
	if err := rotateFile(path, maxBytes, backups); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	size := int64(0)
	if info, err := file.Stat(); err == nil {
		size = info.Size()
	}
	return &RotatingFile{path: path, maxBytes: maxBytes, backups: backups, file: file, size: size}, nil
}

func (w *RotatingFile) Size() int64 {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.size
}

func (w *RotatingFile) Write(data []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.file == nil {
		return 0, os.ErrClosed
	}
	written := 0
	for len(data) > 0 {
		if w.size >= w.maxBytes && !time.Now().Before(w.retryRotateAt) {
			if err := w.rotateLocked(); err != nil {
				return written, err
			}
		}
		chunk := data
		if remaining := w.maxBytes - w.size; remaining > 0 && int64(len(chunk)) > remaining {
			chunk = chunk[:remaining]
		}
		n, err := w.file.Write(chunk)
		written += n
		w.size += int64(n)
		data = data[n:]
		if err != nil {
			return written, err
		}
		if n == 0 {
			return written, io.ErrShortWrite
		}
	}
	return written, nil
}

// rotateRetryDelay spaces out rotation attempts when a rename keeps failing,
// for example while another process briefly has the log open on Windows.
const rotateRetryDelay = time.Minute

func (w *RotatingFile) rotateLocked() error {
	if err := w.file.Close(); err != nil {
		return err
	}
	w.file = nil
	rotateErr := rotateFile(w.path, w.maxBytes, w.backups)
	file, err := os.OpenFile(w.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return errors.Join(rotateErr, err)
	}
	w.file = file
	if rotateErr != nil {
		// Keep logging to the current file and try again later, rather than
		// losing every later line because one rename failed.
		w.retryRotateAt = time.Now().Add(rotateRetryDelay)
		if info, statErr := file.Stat(); statErr == nil {
			w.size = info.Size()
		}
		return nil
	}
	w.size = 0
	return nil
}

func (w *RotatingFile) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.file == nil {
		return nil
	}
	err := w.file.Close()
	w.file = nil
	return err
}

func rotateFile(path string, maxBytes int64, backups int) error {
	if strings.TrimSpace(path) == "" || maxBytes <= 0 || backups < 1 {
		return fmt.Errorf("invalid log rotation settings")
	}
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return fmt.Errorf("log file is not a regular file")
	}
	if info.Size() < maxBytes {
		return nil
	}
	for index := backups; index >= 1; index-- {
		destination := fmt.Sprintf("%s.%d", path, index)
		if err := os.Remove(destination); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		if index == 1 {
			if err := os.Rename(path, destination); err != nil {
				return err
			}
			continue
		}
		source := fmt.Sprintf("%s.%d", path, index-1)
		if err := os.Rename(source, destination); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	return nil
}
