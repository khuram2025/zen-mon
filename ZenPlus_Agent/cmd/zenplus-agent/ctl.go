package main

// Local administration commands, formerly the separate zenplus-agentctl.exe.
// That program linked the same packages as the agent itself, so it cost a
// second ~8 MB binary for a handful of subcommands. The installer adds a
// zenplus-agentctl.cmd shim so existing scripts keep working.

import (
	"encoding/json"
	"flag"
	"fmt"
	"time"

	"zenplus-agent/internal/agent"
	"zenplus-agent/internal/appstate"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
)

// runCtlCommand handles the administration subcommands and reports whether
// cmd was one of them.
func runCtlCommand(cmd string, args []string) (bool, error) {
	switch cmd {
	case "status":
		fs := flag.NewFlagSet("status", flag.ExitOnError)
		configPath := fs.String("config", config.DefaultConfigPath, "agent config path")
		jsonOut := fs.Bool("json", false, "print raw JSON")
		_ = fs.Parse(args)
		status, err := agent.ReadStatus(*configPath)
		if err != nil {
			return true, err
		}
		if *jsonOut {
			b, _ := json.MarshalIndent(status, "", "  ")
			fmt.Println(string(b))
			return true, nil
		}
		printStatus(status)
		return true, nil
	case "print-config":
		fs := flag.NewFlagSet("print-config", flag.ExitOnError)
		configPath := fs.String("config", config.DefaultConfigPath, "agent config path")
		_ = fs.Parse(args)
		return true, agent.PrintConfig(*configPath)
	case "service-status":
		printServiceStatus(appstate.ReadServiceSnapshot())
		return true, nil
	case "reset-enrollment":
		fs := flag.NewFlagSet("reset-enrollment", flag.ExitOnError)
		configPath := fs.String("config", config.DefaultConfigPath, "agent config path")
		force := fs.Bool("force", false, "reset without confirmation")
		_ = fs.Parse(args)
		if !*force {
			return true, fmt.Errorf("reset-enrollment requires --force")
		}
		return true, agent.ResetEnrollment(*configPath)
	default:
		return false, nil
	}
}

func printStatus(s model.Status) {
	fmt.Printf("ZenPlus Agent %s\n", s.AgentVersion)
	fmt.Printf("Agent ID:     %s\n", valueOrDash(s.AgentID))
	fmt.Printf("Server ID:    %s\n", valueOrDash(s.ServerID))
	fmt.Printf("Controller:   %s\n", valueOrDash(s.ControllerURL))
	fmt.Printf("Started:      %s\n", s.StartedAt.Format(time.RFC3339))
	fmt.Printf("Queue depth:  %d\n", s.QueueDepth)
	fmt.Printf("Spool bytes:  %d\n", s.SpoolBytes)
	fmt.Printf("Quarantined:  %d batch(es), %d metadata bytes (%d original payload bytes)\n",
		s.QuarantinedBatches, s.QuarantinedBytes, s.QuarantinedOriginalBytes)
	printTime("Upload retry", s.NextUploadRetryAt)
	printTime("Collection", s.LastCollection)
	printTime("Heartbeat", s.LastHeartbeat)
	if s.LastHeartbeatError != "" {
		fmt.Printf("Heartbeat err: %s\n", s.LastHeartbeatError)
	}
	printTime("Upload", s.LastUpload)
	if s.LastUploadError != "" {
		fmt.Printf("Upload err:   %s\n", s.LastUploadError)
	}
	if len(s.CollectorErrors) > 0 {
		fmt.Println("Collector errors:")
		for k, v := range s.CollectorErrors {
			fmt.Printf("  %s: %s\n", k, v)
		}
	}
}

func printTime(label string, t *time.Time) {
	if t == nil {
		fmt.Printf("%-12s -\n", label+":")
		return
	}
	fmt.Printf("%-12s %s\n", label+":", t.Format(time.RFC3339))
}

func printServiceStatus(s appstate.ServiceSnapshot) {
	fmt.Printf("Service:   %s\n", valueOrDash(s.Name))
	if !s.Installed {
		if s.Error != "" {
			fmt.Printf("Status:    unavailable (%s)\n", s.Error)
			return
		}
		fmt.Println("Status:    not installed")
		return
	}
	fmt.Printf("Status:    %s\n", valueOrDash(s.State))
	fmt.Printf("Start:     %s\n", valueOrDash(s.StartMode))
}

func valueOrDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}
