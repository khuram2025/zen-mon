package store

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/redis/go-redis/v9"
	"github.com/zenplus/poller/internal/checker"
	"github.com/zenplus/poller/internal/config"
	"github.com/zenplus/poller/internal/pinger"
)

// RedisStore handles real-time event publishing via Redis pub/sub.
type RedisStore struct {
	client *redis.Client
}

const (
	ChannelMetrics             = "zenplus:metrics"
	ChannelStatusChange        = "zenplus:status_change"
	ChannelAlerts              = "zenplus:alerts"
	ChannelServiceMetrics      = "zenplus:service_metrics"
	ChannelServiceStatusChange = "zenplus:service_status_change"
)

// NewRedisStore connects to Redis.
func NewRedisStore(cfg *config.Config) (*RedisStore, error) {
	client := redis.NewClient(&redis.Options{
		Addr:     fmt.Sprintf("%s:%d", cfg.Redis.Host, cfg.Redis.Port),
		Password: cfg.Redis.Password,
		DB:       cfg.Redis.DB,
	})

	if err := client.Ping(context.Background()).Err(); err != nil {
		return nil, fmt.Errorf("ping redis: %w", err)
	}

	return &RedisStore{client: client}, nil
}

// Close closes the Redis connection.
func (s *RedisStore) Close() {
	s.client.Close()
}

// PublishMetric publishes a ping result to the metrics channel.
func (s *RedisStore) PublishMetric(ctx context.Context, result *pinger.PingResult) error {
	data, err := json.Marshal(map[string]interface{}{
		"device_id":   result.DeviceID.String(),
		"ip_address":  result.IPAddress.String(),
		"is_up":       result.IsUp,
		"rtt_ms":      float64(result.RTT.Microseconds()) / 1000.0,
		"packet_loss": result.PacketLoss,
		"timestamp":   result.Timestamp,
	})
	if err != nil {
		return err
	}
	return s.client.Publish(ctx, ChannelMetrics, data).Err()
}

// PublishStatusChange publishes a status change event.
func (s *RedisStore) PublishStatusChange(ctx context.Context, sc *pinger.StatusChange) error {
	data, err := json.Marshal(map[string]interface{}{
		"device_id":  sc.DeviceID.String(),
		"old_status": sc.OldStatus,
		"new_status": sc.NewStatus,
		"reason":     sc.Reason,
		"timestamp":  sc.Timestamp,
	})
	if err != nil {
		return err
	}
	return s.client.Publish(ctx, ChannelStatusChange, data).Err()
}

// PublishServiceMetric publishes a service check result.
func (s *RedisStore) PublishServiceMetric(ctx context.Context, result *checker.ServiceCheckResult) error {
	data, err := json.Marshal(map[string]interface{}{
		"service_check_id": result.ServiceCheckID.String(),
		"check_type":       result.CheckType,
		"is_up":            result.IsUp,
		"response_ms":      float64(result.ResponseTime.Microseconds()) / 1000.0,
		"status_code":      result.StatusCode,
		"error":            result.Error,
		"timestamp":        result.Timestamp,
	})
	if err != nil {
		return err
	}
	return s.client.Publish(ctx, ChannelServiceMetrics, data).Err()
}

// PublishServiceStatusChange publishes a service check status change.
func (s *RedisStore) PublishServiceStatusChange(ctx context.Context, sc *checker.ServiceStatusChange) error {
	data, err := json.Marshal(map[string]interface{}{
		"service_check_id": sc.ServiceCheckID.String(),
		"check_type":       sc.CheckType,
		"old_status":       sc.OldStatus,
		"new_status":       sc.NewStatus,
		"reason":           sc.Reason,
		"timestamp":        sc.Timestamp,
	})
	if err != nil {
		return err
	}
	return s.client.Publish(ctx, ChannelServiceStatusChange, data).Err()
}
