package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"zenplus-agent/internal/agent"
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
	cmd := "run"
	if len(os.Args) > 1 {
		cmd = os.Args[1]
	}
	switch cmd {
	case "run":
		fs := flag.NewFlagSet("run", flag.ExitOnError)
		configPath := fs.String("config", config.DefaultConfigPath, "agent config path")
		once := fs.Bool("once", false, "collect, heartbeat/upload once, then exit")
		duration := fs.Duration("duration", 0, "optional run duration, for example 2m")
		_ = fs.Parse(os.Args[2:])
		return agent.Run(context.Background(), agent.Options{ConfigPath: *configPath, Once: *once, Duration: *duration, Foreground: true})
	case "service":
		fs := flag.NewFlagSet("service", flag.ExitOnError)
		configPath := fs.String("config", installedConfigPath(), "agent config path")
		_ = fs.Parse(os.Args[2:])
		return runService(*configPath)
	case "install-service":
		fs := flag.NewFlagSet("install-service", flag.ExitOnError)
		configPath := fs.String("config", installedConfigPath(), "agent config path")
		_ = fs.Parse(os.Args[2:])
		exe, err := os.Executable()
		if err != nil {
			return err
		}
		return installService(exe, *configPath)
	case "uninstall-service":
		return uninstallService()
	case "collect-now":
		fs := flag.NewFlagSet("collect-now", flag.ExitOnError)
		configPath := fs.String("config", config.DefaultConfigPath, "agent config path")
		timeout := fs.Duration("timeout", 45*time.Second, "collection timeout")
		_ = fs.Parse(os.Args[2:])
		ctx, cancel := context.WithTimeout(context.Background(), *timeout)
		defer cancel()
		return agent.CollectNow(ctx, *configPath)
	case "enroll", "re-enroll", "reenroll":
		fs := flag.NewFlagSet("enroll", flag.ExitOnError)
		configPath := fs.String("config", config.DefaultConfigPath, "agent config path")
		token := fs.String("token", "", "one-time enrollment token")
		timeout := fs.Duration("timeout", 45*time.Second, "enrollment timeout")
		_ = fs.Parse(os.Args[2:])
		ctx, cancel := context.WithTimeout(context.Background(), *timeout)
		defer cancel()
		result, err := agent.EnrollNow(ctx, *configPath, *token)
		if err != nil {
			return err
		}
		fmt.Printf("enrolled agent_id=%s server_id=%s\n", result.Identity.AgentID, result.Identity.ServerID)
		return nil
	case "version":
		fmt.Println(model.AgentVersion)
		return nil
	default:
		return fmt.Errorf("unknown command %q\nusage: %s [run|service|install-service|uninstall-service|collect-now|enroll|version]", cmd, filepath.Base(os.Args[0]))
	}
}

func installedConfigPath() string {
	if runtime.GOOS == "linux" {
		return "/etc/zenplus-agent/agent.yaml"
	}
	programData := os.Getenv("ProgramData")
	if programData == "" {
		return config.DefaultConfigPath
	}
	return filepath.Join(programData, "ZenPlus", "Agent", "config", "agent.yaml")
}
