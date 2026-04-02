package store

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/redis/go-redis/v9"
	"github.com/zenplus/poller/internal/config"
	"github.com/zenplus/poller/internal/pinger"
)

// RedisStore handles real-time event publishing via Redis pub/sub.
type RedisStore struct {
	client *redis.Client
}

const (
	ChannelMetrics      = "zenplus:metrics"
	ChannelStatusChange = "zenplus:status_change"
	ChannelAlerts       = "zenplus:alerts"
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
