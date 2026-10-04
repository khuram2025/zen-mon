package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/zenplus/poller/internal/netflow"
)

// templateStateFile keeps the NetFlow v9 / IPFIX template caches (templates,
// options templates, samplers, system init times) across restarts, next to
// the disk spool in the collector's state directory. Without it every restart
// dropped each exporter's data sets until it re-announced its templates (up
// to its template timeout, 30 minutes by default on Cisco) and stored sampled
// traffic unscaled until the sampler options arrived again (see
// netflow.CacheSnapshot).
const templateStateFile = "templates.json"

// templateStateRefresh: the file is rewritten when a cache changed, and at
// least this often while templates are being re-announced, so the saved
// announcement times stay close to the live ones (the TTL applies to them
// after a restart).
const templateStateRefresh = time.Hour

const templateStateVersion = 1

type templateState struct {
	Version  int                   `json:"version"`
	SavedAt  time.Time             `json:"saved_at"`
	NetFlow9 netflow.CacheSnapshot `json:"netflow9"`
	IPFIX    netflow.CacheSnapshot `json:"ipfix"`
}

// loadTemplateState restores the caches saved by the previous run. A missing
// file is a first start; an unreadable one is reported and ignored (the
// exporters' next announcements rebuild the caches as before).
func (c *collector) loadTemplateState() {
	path := filepath.Join(c.cfg.SpoolDir, templateStateFile)
	data, err := os.ReadFile(path)
	if err != nil {
		if !os.IsNotExist(err) {
			fmt.Printf("WARN: netflow template state %s unreadable: %v\n", path, err)
		}
		return
	}
	var st templateState
	if err := json.Unmarshal(data, &st); err != nil || st.Version != templateStateVersion {
		fmt.Printf("WARN: netflow template state %s ignored (format %d, %v); templates are relearned from the exporters\n", path, st.Version, err)
		return
	}
	t9, s9 := c.v9Templates.Restore(st.NetFlow9)
	tx, sx := c.ipfixTemplates.Restore(st.IPFIX)
	c.tplSavedV9, c.tplSavedIPFIX, c.tplSavedAt = c.v9Templates.Version(), c.ipfixTemplates.Version(), st.SavedAt
	fmt.Printf("INFO: restored %d NetFlow v9 and %d IPFIX templates, %d samplers, saved %s\n",
		t9, tx, s9+sx, st.SavedAt.Format(time.RFC3339))
}

// saveTemplateState writes the caches when they changed since the last save
// (or the last save is older than templateStateRefresh, or force). The file
// is replaced atomically, so a crash mid-write leaves the previous one.
func (c *collector) saveTemplateState(now time.Time, force bool) {
	if c.cfg.SpoolDir == "" || c.spool == nil {
		return
	}
	v9, vx := c.v9Templates.Version(), c.ipfixTemplates.Version()
	if !force && v9 == c.tplSavedV9 && vx == c.tplSavedIPFIX && now.Sub(c.tplSavedAt) < templateStateRefresh {
		return
	}
	data, err := json.Marshal(templateState{
		Version:  templateStateVersion,
		SavedAt:  now.UTC(),
		NetFlow9: c.v9Templates.Snapshot(),
		IPFIX:    c.ipfixTemplates.Snapshot(),
	})
	if err == nil {
		err = writeFileAtomic(filepath.Join(c.cfg.SpoolDir, templateStateFile), data)
	}
	if err != nil {
		c.logf(&c.lastTemplateStateLog, "WARN: netflow template state not saved: %v", err)
		return
	}
	c.tplSavedV9, c.tplSavedIPFIX, c.tplSavedAt = v9, vx, now
}

// writeFileAtomic writes data to a temp file in the same directory, syncs it
// and renames it over path.
func writeFileAtomic(path string, data []byte) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".tmp-state-*")
	if err != nil {
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmp.Name())
		return err
	}
	if err := os.Chmod(tmp.Name(), 0o640); err != nil {
		os.Remove(tmp.Name())
		return err
	}
	if err := os.Rename(tmp.Name(), path); err != nil {
		os.Remove(tmp.Name())
		return err
	}
	return nil
}
