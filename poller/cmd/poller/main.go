package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/zenplus/poller/internal/config"
	"github.com/zenplus/poller/internal/pinger"
	"github.com/zenplus/poller/internal/store"
	"go.uber.org/zap"
)

func main() {
	// Initialize logger
	logger, _ := zap.NewProduction()
	if os.Getenv("LOG_LEVEL") == "debug" {
		logger, _ = zap.NewDevelopment()
	}
	defer logger.Sync()

	sugar := logger.Sugar()
	sugar.Info("Starting ZenPlus Poller")

	// Load configuration
	cfg, err := config.Load()
	if err != nil {
		sugar.Fatalf("Failed to load configuration: %v", err)
	}
	sugar.Infof("Configuration loaded: poller_id=%s", cfg.Poller.ID)

	// Create context with cancellation
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Connect to PostgreSQL
	pgStore, err := store.NewPostgresStore(ctx, cfg)
	if err != nil {
		sugar.Fatalf("Failed to connect to PostgreSQL: %v", err)
	}
	defer pgStore.Close()
	sugar.Info("Connected to PostgreSQL")

	// Connect to ClickHouse
	chStore, err := store.NewClickHouseStore(cfg)
	if err != nil {
		sugar.Fatalf("Failed to connect to ClickHouse: %v", err)
	}
	defer chStore.Close()
	sugar.Info("Connected to ClickHouse")

	// Connect to Redis
	redisStore, err := store.NewRedisStore(cfg)
	if err != nil {
		sugar.Fatalf("Failed to connect to Redis: %v", err)
	}
	defer redisStore.Close()
	sugar.Info("Connected to Redis")

	// Create ping engine
	engine, err := pinger.NewEngine(cfg, pgStore, chStore, redisStore, sugar)
	if err != nil {
		sugar.Fatalf("Failed to create ping engine: %v", err)
	}

	// Start the engine
	sugar.Info("Starting ping engine...")
	go engine.Run(ctx)

	// Start health check server
	go func() {
		if err := startHealthServer(cfg.Health.Port, engine); err != nil {
			sugar.Errorf("Health server error: %v", err)
		}
	}()

	sugar.Infof("ZenPlus Poller is running (health: :%d)", cfg.Health.Port)

	// Wait for shutdown signal
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	sig := <-sigChan

	sugar.Infof("Received signal %v, shutting down...", sig)
	cancel()

	// Allow graceful shutdown
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()

	engine.Shutdown(shutdownCtx)
	sugar.Info("ZenPlus Poller stopped")
}

func startHealthServer(port int, engine *pinger.Engine) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		status := engine.HealthStatus()
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(status)
	})
	return http.ListenAndServe(fmt.Sprintf(":%d", port), mux)
}
