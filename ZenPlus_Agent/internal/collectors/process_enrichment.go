package collectors

import (
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/shirou/gopsutil/v4/process"
)

const (
	maxProcessCommandLineBytes = 2048
	maxProcessCommandArgs      = 64
	maxProcessTextBytes        = 255
)

type processSample struct {
	process    *process.Process
	PID        int32
	Name       string
	CPUPercent float64
	RSS        uint64
	Threads    int32
	Handles    int32
	UserName   string
	CreatedMs  int64
}

var sensitiveProcessOptions = map[string]bool{
	"--access-key": true, "--api-key": true, "--apikey": true,
	"--authorization": true, "--client-secret": true,
	"--connection-string": true, "--credential": true,
	"--password": true, "--passwd": true, "--pwd": true,
	"--secret": true, "--token": true,
	"-p": true, "-u": true,
}

var safeProcessOptions = map[string]bool{
	"--bind": true, "--config": true, "--debug": true,
	"--environment": true, "--host": true, "--instance": true,
	"--listen": true, "--mode": true, "--port": true,
	"--profile": true, "--service": true, "--verbose": true,
	"-c": true, "-d": true, "-h": true, "-v": true,
}

// safeProcessCommandLine emits a command shape, never argument values. This
// retains useful launch-mode hints while preventing credentials, private file
// paths, SQL text, and other user input from entering monitoring telemetry.
func safeProcessCommandLine(processName string, argv []string) string {
	processName = truncateUTF8Bytes(strings.TrimSpace(processName), maxProcessTextBytes)
	if processName == "" {
		return ""
	}
	parts := []string{processName}
	if len(argv) > 1 {
		argv = argv[1:]
	} else {
		argv = nil
	}
	if len(argv) > maxProcessCommandArgs {
		argv = argv[:maxProcessCommandArgs]
	}
	for _, argument := range argv {
		parts = append(parts, processArgumentShape(argument))
	}
	return truncateUTF8Bytes(strings.Join(parts, " "), maxProcessCommandLineBytes)
}

func processArgumentShape(argument string) string {
	argument = strings.TrimSpace(argument)
	if argument == "" {
		return "[ARG]"
	}
	key := argument
	hasValue := false
	if index := strings.IndexAny(key, "=:"); index > 0 {
		key = key[:index]
		hasValue = true
	}
	key = strings.ToLower(key)
	if sensitiveProcessOptions[key] {
		return key + "=[REDACTED]"
	}
	if safeProcessOptions[key] {
		if hasValue {
			return key + "=[VALUE]"
		}
		return key
	}
	if strings.HasPrefix(argument, "-") || strings.HasPrefix(argument, "/") {
		return "[OPTION]"
	}
	return "[ARG]"
}

func processStartedAt(createdMs int64) string {
	if createdMs <= 0 {
		return ""
	}
	return time.UnixMilli(createdMs).UTC().Format(time.RFC3339Nano)
}

func normalizedProcessWatchlist(values []string) []string {
	unique := make(map[string]struct{}, len(values))
	for _, value := range values {
		value = strings.ToLower(strings.TrimSpace(value))
		value = truncateUTF8Bytes(value, maxProcessTextBytes)
		if value != "" {
			unique[value] = struct{}{}
		}
	}
	out := make([]string, 0, len(unique))
	for value := range unique {
		out = append(out, value)
	}
	sort.Strings(out)
	return out
}

func missingWatchedProcesses(watchlist []string, samples []processSample) []string {
	present := make(map[string]bool, len(samples))
	for _, sample := range samples {
		name := strings.ToLower(strings.TrimSpace(sample.Name))
		if name != "" {
			present[name] = true
		}
	}
	missing := make([]string, 0, len(watchlist))
	for _, name := range normalizedProcessWatchlist(watchlist) {
		if !present[name] {
			missing = append(missing, name)
		}
	}
	return missing
}

func truncateUTF8Bytes(value string, maxBytes int) string {
	if maxBytes <= 0 {
		return ""
	}
	if len(value) <= maxBytes {
		return value
	}
	end := maxBytes
	for end > 0 && !utf8.ValidString(value[:end]) {
		end--
	}
	return strings.TrimSpace(value[:end])
}
