package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"zenplus-agent/internal/agent"
	"zenplus-agent/internal/appstate"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	cmd := "status"
	if len(os.Args) > 1 {
		cmd = os.Args[1]
	}
	switch cmd {
	case "status":
		fs := flag.NewFlagSet("status", flag.ExitOnError)
		configPath := fs.String("config", config.DefaultConfigPath, "agent config path")
		jsonOut := fs.Bool("json", false, "print raw JSON")
		_ = fs.Parse(os.Args[2:])
		status, err := agent.ReadStatus(*configPath)
		if err != nil {
			return err
		}
		if *jsonOut {
			b, _ := json.MarshalIndent(status, "", "  ")
			fmt.Println(string(b))
			return nil
		}
		printStatus(status)
		return nil
	case "collect-now":
		fs := flag.NewFlagSet("collect-now", flag.ExitOnError)
		configPath := fs.String("config", config.DefaultConfigPath, "agent config path")
		timeout := fs.Duration("timeout", 45*time.Second, "collection timeout")
		_ = fs.Parse(os.Args[2:])
		ctx, cancel := context.WithTimeout(context.Background(), *timeout)
		defer cancel()
		return agent.CollectNow(ctx, *configPath)
	case "print-config":
		fs := flag.NewFlagSet("print-config", flag.ExitOnError)
		configPath := fs.String("config", config.DefaultConfigPath, "agent config path")
		_ = fs.Parse(os.Args[2:])
		return agent.PrintConfig(*configPath)
	case "service-status":
		printServiceStatus(appstate.ReadServiceSnapshot())
		return nil
	case "reset-enrollment":
		fs := flag.NewFlagSet("reset-enrollment", flag.ExitOnError)
		configPath := fs.String("config", config.DefaultConfigPath, "agent config path")
		force := fs.Bool("force", false, "reset without confirmation")
		_ = fs.Parse(os.Args[2:])
		if !*force {
			return fmt.Errorf("reset-enrollment requires --force")
		}
		return agent.ResetEnrollment(*configPath)
	case "register":
		fs := flag.NewFlagSet("register", flag.ExitOnError)
		configPath := fs.String("config", config.DefaultConfigPath, "agent config path")
		timeout := fs.Duration("timeout", 45*time.Second, "registration timeout")
		_ = fs.Parse(os.Args[2:])
		ctx, cancel := context.WithTimeout(context.Background(), *timeout)
		defer cancel()
		result, err := agent.RegisterNow(ctx, *configPath)
		if err != nil {
			return err
		}
		fmt.Printf("registration_state=%s agent_id=%s server_id=%s\n", result.AuthorizationState, result.Identity.AgentID, result.Identity.ServerID)
		return nil
	case "version":
		fmt.Println(model.AgentVersion)
		return nil
	default:
		return fmt.Errorf("unknown command %q\nusage: %s [status|collect-now|print-config|service-status|reset-enrollment|register|version]", cmd, filepath.Base(os.Args[0]))
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
