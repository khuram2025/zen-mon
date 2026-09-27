//go:build windows && installerpayload

package main

import (
	"archive/zip"
	"bytes"
	_ "embed"
	"fmt"
	"io"
	"io/fs"
	"strings"
)

// payload.zip is produced by scripts\build.ps1 from the staged product tree
// (server binaries, the uninstaller and, in the full flavour, the APM bundle).
// Go's embed stores data uncompressed, so shipping one deflate archive instead
// of the raw tree shrinks the setup program and the MSI that wraps it several
// times over. Every entry is still verified by validateEmbeddedPayloads (and
// the signed APM bundle manifest) before anything is written.
//
//go:embed payload.zip
var payloadZip []byte

// maxPayloadEntryBytes bounds a single decompressed entry. The largest file,
// the telemetry gateway, is about 45 MB.
const maxPayloadEntryBytes = 256 << 20

func embeddedPayloads() ([]payloadFile, error) {
	archive, err := zip.NewReader(bytes.NewReader(payloadZip), int64(len(payloadZip)))
	if err != nil {
		return nil, fmt.Errorf("open embedded installer payload: %w", err)
	}
	payloads := make([]payloadFile, 0, len(archive.File))
	for _, entry := range archive.File {
		// Archives written on Windows may use backslash separators.
		name := strings.ReplaceAll(entry.Name, `\`, "/")
		if strings.HasSuffix(name, "/") || entry.FileInfo().IsDir() {
			continue
		}
		if entry.UncompressedSize64 > maxPayloadEntryBytes {
			return nil, fmt.Errorf("embedded installer payload %q is too large", name)
		}
		data, err := readPayloadEntry(entry)
		if err != nil {
			return nil, fmt.Errorf("read embedded installer payload %q: %w", name, err)
		}
		mode := fs.FileMode(0o644)
		if strings.HasSuffix(strings.ToLower(name), ".exe") {
			mode = 0o755
		}
		payloads = append(payloads, payloadFile{Name: name, Data: data, Mode: mode})
	}
	return payloads, nil
}

func readPayloadEntry(entry *zip.File) ([]byte, error) {
	reader, err := entry.Open()
	if err != nil {
		return nil, err
	}
	defer reader.Close()
	// zip.File.Open verifies the entry's CRC-32 once the stream is drained.
	data, err := io.ReadAll(io.LimitReader(reader, maxPayloadEntryBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > maxPayloadEntryBytes {
		return nil, fmt.Errorf("entry exceeds %d bytes", maxPayloadEntryBytes)
	}
	return data, nil
}
