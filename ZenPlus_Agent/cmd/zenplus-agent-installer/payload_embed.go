//go:build windows && installerpayload

package main

import (
	"embed"
	"io/fs"
)

//go:embed payload
var payloadFS embed.FS

func embeddedPayloads() ([]payloadFile, error) {
	payloads := make([]payloadFile, 0, 16)
	err := fs.WalkDir(payloadFS, "payload", func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		data, err := payloadFS.ReadFile(path)
		if err != nil {
			return err
		}
		name := path[len("payload/"):]
		mode := fs.FileMode(0o644)
		if len(name) >= 4 && name[len(name)-4:] == ".exe" {
			mode = 0o755
		}
		payloads = append(payloads, payloadFile{Name: name, Data: data, Mode: mode})
		return nil
	})
	return payloads, err
}
