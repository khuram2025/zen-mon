package main

import (
	"testing"

	"zenplus-agent/internal/appstate"
	"zenplus-agent/internal/brand"
	"zenplus-agent/internal/model"
)

func TestConfigErrorsFromAnUnreachableControllerAreNotAlarming(t *testing.T) {
	unreachable := `Get "https://10.11.40.36/api/v1/agents/config": dial tcp 10.11.40.36:443: connectex: A connection attempt failed because the connected party did not properly respond after a period of time, or established connection failed because connected host has failed to respond.`
	if text, c := collectorIssueLine(&model.Status{LastConfigError: unreachable}); c != amber || text == "" {
		t.Fatalf("unreachable controller shown as %q", text)
	}
	if _, c := collectorIssueLine(&model.Status{LastConfigError: "config signature verification failed"}); c != red {
		t.Fatal("a rejected configuration must stay red")
	}
}

func TestStatusGlyphFollowsHealth(t *testing.T) {
	running := appstate.Snapshot{Service: appstate.ServiceSnapshot{Installed: true, Running: true}}
	stopped := appstate.Snapshot{Service: appstate.ServiceSnapshot{Installed: true}}
	cases := []struct {
		snap         appstate.Snapshot
		health, tone string
		want         brand.Glyph
	}{
		{running, "Healthy", "ok", brand.GlyphCheck},
		{running, "Connecting", "warn", brand.GlyphPending},
		{running, "Spooling", "warn", brand.GlyphAlert},
		{running, "Controller unreachable", "bad", brand.GlyphCross},
		{stopped, "Service stopped", "bad", brand.GlyphStopped},
	}
	for _, c := range cases {
		if got := statusGlyphFor(c.snap, c.health, c.tone); got != c.want {
			t.Errorf("%s/%s = %v, want %v", c.health, c.tone, got, c.want)
		}
	}
}
