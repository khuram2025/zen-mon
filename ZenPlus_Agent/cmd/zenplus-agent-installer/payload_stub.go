//go:build windows && !installerpayload

package main

import "errors"

func embeddedPayloads() ([]payloadFile, error) {
	return nil, errors.New("installer payload is missing; run scripts\\build.ps1 to build the setup executable")
}
