package store

import (
	"context"
	"os"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// UpsertUdtData wraps the LLDP/CDP topology write in a savepoint so a
// neighbor string the database refuses cannot discard the FDB, port
// state and VLANs collected in the same transaction. pgx implements
// nested Begin as SAVEPOINT, and a failed SendBatch leaves the
// connection in a state that ROLLBACK TO SAVEPOINT has to clear — this
// verifies that combination against a real server rather than assuming.
//
// Run with: UDT_TEST_DSN="postgres://..." go test ./internal/store/ -run Savepoint -v
func TestSavepointSurvivesFailedBatch(t *testing.T) {
	dsn := os.Getenv("UDT_TEST_DSN")
	if dsn == "" {
		t.Skip("UDT_TEST_DSN not set")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer pool.Close()

	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	// Everything happens in a transaction that is always rolled back, so
	// this never touches real data.
	defer tx.Rollback(ctx)

	if _, err := tx.Exec(ctx, `CREATE TEMP TABLE sp_probe (v TEXT) ON COMMIT DROP`); err != nil {
		t.Fatalf("create temp table: %v", err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO sp_probe VALUES ('before')`); err != nil {
		t.Fatalf("seed insert: %v", err)
	}

	// Mimic upsertTopologyLinks: a batch whose payload Postgres rejects
	// with SQLSTATE 22021, exactly as the NUL-bearing chassis IDs did.
	batchErr := func() error {
		sp, err := tx.Begin(ctx)
		if err != nil {
			return err
		}
		batch := &pgx.Batch{}
		batch.Queue(`INSERT INTO sp_probe VALUES ($1)`, "ok-row")
		batch.Queue(`INSERT INTO sp_probe VALUES ($1)`, "bad\x00row")
		br := sp.SendBatch(ctx, batch)
		var qerr error
		for i := 0; i < 2; i++ {
			if _, err := br.Exec(); err != nil && qerr == nil {
				qerr = err
			}
		}
		_ = br.Close()
		if qerr != nil {
			_ = sp.Rollback(ctx)
			return qerr
		}
		return sp.Commit(ctx)
	}()

	if batchErr == nil {
		t.Fatal("expected the NUL-bearing insert to be rejected")
	}
	if !strings.Contains(batchErr.Error(), "22021") &&
		!strings.Contains(strings.ToLower(batchErr.Error()), "invalid byte sequence") {
		t.Logf("note: got a different rejection than 22021: %v", batchErr)
	}
	t.Logf("batch rejected as expected: %v", batchErr)

	// The whole point: the outer transaction must still be usable.
	if _, err := tx.Exec(ctx, `INSERT INTO sp_probe VALUES ('after')`); err != nil {
		t.Fatalf("outer transaction was aborted by the failed batch: %v", err)
	}
	var n int
	if err := tx.QueryRow(ctx, `SELECT count(*) FROM sp_probe`).Scan(&n); err != nil {
		t.Fatalf("count: %v", err)
	}
	// 'before' and 'after' survive; both batch rows rolled back.
	if n != 2 {
		t.Errorf("expected 2 surviving rows (before, after), got %d", n)
	}
}
