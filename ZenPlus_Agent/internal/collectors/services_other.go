//go:build !windows && !linux

package collectors

import "errors"

func listServices() ([]serviceInfo, error) {
	return nil, errors.New("service collection is not available on this platform")
}
