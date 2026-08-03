//go:build windows && installerpayload

package main

import (
	"embed"
	"io/fs"
)

//go:embed payload/zenplus-agent.exe payload/zenplus-agentctl.exe payload/zenplus-agent-app.exe payload/zenplus-agent-user.exe
var payloadFS embed.FS

func embeddedPayloads() ([]payloadFile, error) {
	names := []string{
		"zenplus-agent.exe",
		"zenplus-agentctl.exe",
		"zenplus-agent-app.exe",
		"zenplus-agent-user.exe",
	}
	payloads := make([]payloadFile, 0, len(names))
	for _, name := range names {
		data, err := payloadFS.ReadFile("payload/" + name)
		if err != nil {
			return nil, err
		}
		payloads = append(payloads, payloadFile{Name: name, Data: data, Mode: fs.FileMode(0o755)})
	}
	return payloads, nil
}
