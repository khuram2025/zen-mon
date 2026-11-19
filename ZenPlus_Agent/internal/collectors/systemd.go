package collectors

// Portable parsing for the Linux sources: systemd service units, the load
// average and the boot time. The Linux collectors (services_linux.go,
// lifecycle_linux.go) feed these from systemctl and /proc; keeping the
// parsers free of build tags lets every platform's CI test them.

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
)

// commandRunner runs a program and returns its standard output. A non-zero
// exit status is returned together with whatever the program printed, because
// `systemctl is-system-running` reports its answer that way.
type commandRunner func(ctx context.Context, name string, args ...string) ([]byte, error)

func execRunner(ctx context.Context, name string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Env = append(cmd.Environ(), "SYSTEMD_COLORS=0", "SYSTEMD_PAGER=", "LC_ALL=C")
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		if msg := strings.TrimSpace(stderr.String()); msg != "" {
			return out, fmt.Errorf("%w: %s", err, msg)
		}
	}
	return out, err
}

// systemdServiceProperties are read with one `systemctl show` call for every
// unit list-units returned.
var systemdServiceProperties = []string{
	"Id", "Description", "LoadState", "ActiveState", "SubState",
	"UnitFileState", "MainPID", "ExecMainStatus",
}

// maxSystemdUnitsPerShow bounds one `systemctl show` command line.
const maxSystemdUnitsPerShow = 256

// parseSystemdUnitList reads the unit names from
// `systemctl list-units --type=service --all --no-legend --plain`.
// Each line is "UNIT LOAD ACTIVE SUB DESCRIPTION"; failed or missing units
// may carry a leading "●" (or "*" without UTF-8) marker.
func parseSystemdUnitList(out []byte) []string {
	var units []string
	seen := map[string]bool{}
	scanner := bufio.NewScanner(bytes.NewReader(out))
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) > 0 && (fields[0] == "●" || fields[0] == "*" || fields[0] == "×") {
			fields = fields[1:]
		}
		if len(fields) < 2 || !strings.HasSuffix(fields[0], ".service") {
			continue
		}
		// Units that systemd only knows by reference ("not-found") are not
		// installed services; a watched name still reports not_found.
		if fields[1] == "not-found" || seen[fields[0]] {
			continue
		}
		seen[fields[0]] = true
		units = append(units, fields[0])
	}
	return units
}

// parseSystemdShow splits `systemctl show -p ... unit...` output into one
// property map per unit. Blocks are separated by blank lines.
func parseSystemdShow(out []byte) []map[string]string {
	var blocks []map[string]string
	current := map[string]string{}
	flush := func() {
		if len(current) > 0 {
			blocks = append(blocks, current)
			current = map[string]string{}
		}
	}
	scanner := bufio.NewScanner(bytes.NewReader(out))
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.TrimSpace(line) == "" {
			flush()
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		current[key] = value
	}
	flush()
	return blocks
}

// systemdService maps one unit's properties to the service inventory shape
// the Windows agent reports, so the controller, the Services tab and the
// host_service_down rule need no Linux-specific handling.
func systemdService(props map[string]string) (serviceInfo, bool) {
	id := props["Id"]
	if !strings.HasSuffix(id, ".service") {
		return serviceInfo{}, false
	}
	switch props["LoadState"] {
	case "loaded", "masked":
	default:
		return serviceInfo{}, false
	}
	// "nginx" rather than "nginx.service": what operators type and what
	// systemctl accepts, and the form an alert rule target uses.
	name := strings.TrimSuffix(id, ".service")
	if name == "" {
		return serviceInfo{}, false
	}
	display := strings.TrimSpace(props["Description"])
	if display == "" || display == id {
		display = name
	}
	pid, _ := strconv.Atoi(props["MainPID"])
	exitCode, _ := strconv.Atoi(props["ExecMainStatus"])
	state := systemdServiceState(props["ActiveState"])
	if state == "stopped" || state == "failed" {
		pid = 0
	}
	return serviceInfo{
		ServiceName: name,
		DisplayName: display,
		State:       state,
		StartMode:   systemdStartMode(props["UnitFileState"], props["LoadState"]),
		PID:         pid,
		ExitCode:    exitCode,
	}, true
}

// systemdServiceState maps ActiveState to the Windows state vocabulary.
// "active" covers both a running daemon and a oneshot unit that finished and
// remains active (SubState "exited"): both mean the service is up.
// "failed" is kept: the controller treats it as stopped and it tells the
// operator more.
func systemdServiceState(active string) string {
	switch active {
	case "active", "reloading", "refreshing":
		return "running"
	case "activating":
		return "start_pending"
	case "deactivating":
		return "stop_pending"
	case "inactive", "maintenance":
		return "stopped"
	case "failed":
		return "failed"
	default:
		return "unknown"
	}
}

// systemdStartMode maps UnitFileState to the Windows start modes: enabled
// units start at boot (auto); masked units cannot start (disabled);
// disabled, static, indirect, generated and transient units start only when
// something asks for them (manual).
func systemdStartMode(unitFileState, loadState string) string {
	if loadState == "masked" {
		return "disabled"
	}
	switch unitFileState {
	case "enabled", "enabled-runtime", "linked", "linked-runtime", "alias":
		return "auto"
	case "masked", "masked-runtime", "bad":
		return "disabled"
	case "":
		return "unknown"
	default:
		return "manual"
	}
}

// listSystemdServices enumerates service units with two systemctl calls.
func listSystemdServices(ctx context.Context, run commandRunner) ([]serviceInfo, error) {
	out, err := run(ctx, "systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain", "--no-pager")
	if err != nil {
		return nil, fmt.Errorf("systemctl list-units: %w", err)
	}
	units := parseSystemdUnitList(out)
	services := make([]serviceInfo, 0, len(units))
	for start := 0; start < len(units); start += maxSystemdUnitsPerShow {
		end := min(start+maxSystemdUnitsPerShow, len(units))
		args := []string{"show", "--no-pager", "-p", strings.Join(systemdServiceProperties, ",")}
		args = append(args, "--")
		args = append(args, units[start:end]...)
		out, err := run(ctx, "systemctl", args...)
		if err != nil {
			return nil, fmt.Errorf("systemctl show: %w", err)
		}
		for _, props := range parseSystemdShow(out) {
			if svc, ok := systemdService(props); ok {
				services = append(services, svc)
			}
		}
	}
	return services, nil
}

// systemStateStopping reports whether `systemctl is-system-running` says
// "stopping". The command exits non-zero for every state but "running", so
// its output is read whatever the exit status.
func systemStateStopping(ctx context.Context, run commandRunner) bool {
	out, _ := run(ctx, "systemctl", "is-system-running")
	return strings.TrimSpace(string(out)) == "stopping"
}

// errNoSystemd marks a Linux host without systemd: there is no service
// manager to read, which is not a collector failure.
var errNoSystemd = errors.New("systemd is not the service manager on this host")

// loadAverage is the kernel's 1, 5 and 15 minute load average.
type loadAverage struct {
	One, Five, Fifteen float64
}

// procLoadavg is replaced in tests.
var procLoadavg = "/proc/loadavg"

func readLoadAverage() (loadAverage, error) {
	raw, err := os.ReadFile(procLoadavg)
	if err != nil {
		return loadAverage{}, err
	}
	return parseLoadAverage(raw)
}

// parseLoadAverage reads /proc/loadavg ("0.52 0.58 0.59 1/467 12345").
func parseLoadAverage(raw []byte) (loadAverage, error) {
	fields := strings.Fields(string(raw))
	if len(fields) < 3 {
		return loadAverage{}, fmt.Errorf("unexpected /proc/loadavg content %q", strings.TrimSpace(string(raw)))
	}
	var values [3]float64
	for i := range values {
		v, err := strconv.ParseFloat(fields[i], 64)
		if err != nil || v < 0 {
			return loadAverage{}, fmt.Errorf("invalid load average %q", fields[i])
		}
		values[i] = v
	}
	return loadAverage{One: values[0], Five: values[1], Fifteen: values[2]}, nil
}

// parseBootTime returns the "btime" line of /proc/stat: when this boot
// started, in seconds since the epoch.
func parseBootTime(raw []byte) (int64, error) {
	scanner := bufio.NewScanner(bytes.NewReader(raw))
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) == 2 && fields[0] == "btime" {
			v, err := strconv.ParseInt(fields[1], 10, 64)
			if err != nil || v <= 0 {
				return 0, fmt.Errorf("invalid btime %q", fields[1])
			}
			return v, nil
		}
	}
	return 0, errors.New("btime not found in /proc/stat")
}
