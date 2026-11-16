package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"
)

// Device Templates (migration 189): automatic template assignment with a
// recorded source, suggestions when a different template now matches, and
// pilot publishes. While the binary runs ahead of migration 189 every
// statement here falls back to its pre-189 behaviour, so SNMP polling and
// classification never stop because of schema lag.

// recordProfileMatchSQL records that the classifier matched template $2 for
// device $1 ($3 = the template's device type, empty for none). In one UPDATE:
//
//   - a device without a template gets it (profile_source 'auto', stamped),
//     and a device still typed 'other' takes the template's device type
//     (device_type_source 'template');
//   - an assigned template is never changed: a different match is recorded
//     as profile_suggested_id (profile_suggested_at kept while the suggestion
//     is unchanged), and a match equal to the assignment clears the
//     suggestion;
//   - controller-managed children are skipped (they are never polled or
//     classified themselves).
//
// The right-hand sides read the OLD row, so every CASE tests the
// pre-update profile_id. The WHERE clause admits only rows that actually
// change: this runs on every poll of every device.
const recordProfileMatchSQL = `
		UPDATE devices SET
		    profile_id           = COALESCE(profile_id, $2::uuid),
		    profile_source       = CASE WHEN profile_id IS NULL THEN 'auto' ELSE profile_source END,
		    profile_assigned_at  = CASE WHEN profile_id IS NULL THEN now() ELSE profile_assigned_at END,
		    device_type          = CASE WHEN profile_id IS NULL AND ` + profileMatchSetsTypeSQL + `
		                                THEN $3::text ELSE device_type END,
		    device_type_source   = CASE WHEN profile_id IS NULL AND ` + profileMatchSetsTypeSQL + `
		                                THEN 'template' ELSE device_type_source END,
		    profile_suggested_id = CASE WHEN profile_id IS NULL OR profile_id = $2::uuid
		                                THEN NULL ELSE $2::uuid END,
		    profile_suggested_at = CASE WHEN profile_id IS NULL OR profile_id = $2::uuid THEN NULL
		                                WHEN profile_suggested_id = $2::uuid
		                                THEN COALESCE(profile_suggested_at, now())
		                                ELSE now() END
		WHERE id = $1
		  AND managed_by_device_id IS NULL
		  AND poll_mode IS DISTINCT FROM 'via_controller'
		  AND (profile_id IS NULL
		       OR (profile_id = $2::uuid
		           AND (profile_suggested_id IS NOT NULL OR profile_suggested_at IS NOT NULL))
		       OR (profile_id <> $2::uuid
		           AND (profile_suggested_id IS DISTINCT FROM $2::uuid OR profile_suggested_at IS NULL)))`

// profileMatchSetsTypeSQL: the template's device type replaces only the
// catalogue's catch-all 'other', and only a type the catalogue knows (a
// foreign-key failure would otherwise cost the device its template).
const profileMatchSetsTypeSQL = `COALESCE(device_type, 'other') = 'other' AND $3::text <> ''
		                                AND EXISTS (SELECT 1 FROM device_types t WHERE t.key = $3::text)`

// clearProfileSuggestionSQL drops a suggestion once no template matches the
// device any more; it writes only when a suggestion is set.
const clearProfileSuggestionSQL = `
		UPDATE devices
		   SET profile_suggested_id = NULL, profile_suggested_at = NULL
		 WHERE id = $1
		   AND (profile_suggested_id IS NOT NULL OR profile_suggested_at IS NOT NULL)`

// RecordProfileMatch records a classifier match of template profileID
// (device type deviceType, may be empty) for a device: see
// recordProfileMatchSQL. Before migration 189 it assigns the template only
// when the device has none (AssignProfileIfUnset).
func (s *PostgresStore) RecordProfileMatch(ctx context.Context, deviceID, profileID uuid.UUID, deviceType string) error {
	now := time.Now()
	if !s.templatesLegacy(now) {
		_, err := s.pool.Exec(ctx, recordProfileMatchSQL, deviceID, profileID, deviceType)
		if err == nil || !isUndefinedColumn(err) {
			s.noteTemplatesColumnsPresent(err)
			return err
		}
		s.noteTemplatesColumnsMissing(now, err)
	}
	return s.AssignProfileIfUnset(ctx, deviceID, profileID)
}

// ClearProfileSuggestion removes a device's template suggestion, used when
// no template matches it. Before migration 189 there is nothing to clear.
func (s *PostgresStore) ClearProfileSuggestion(ctx context.Context, deviceID uuid.UUID) error {
	now := time.Now()
	if s.templatesLegacy(now) {
		return nil
	}
	_, err := s.pool.Exec(ctx, clearProfileSuggestionSQL, deviceID)
	if err == nil || !isUndefinedColumn(err) {
		s.noteTemplatesColumnsPresent(err)
		return err
	}
	s.noteTemplatesColumnsMissing(now, err)
	return nil
}

// isUndefinedColumn reports whether err is PostgreSQL's undefined_column
// (SQLSTATE 42703).
func isUndefinedColumn(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "42703"
}

// templatesLegacy reports whether the pre-189 statements are in use: a
// missing column was seen less than sysInfoLegacyRetry ago.
func (s *PostgresStore) templatesLegacy(now time.Time) bool {
	return now.UnixNano() < s.templatesLegacyUntil.Load()
}

// noteTemplatesColumnsMissing switches the Device Templates statements to
// their pre-189 form for sysInfoLegacyRetry and warns, at most once per
// sysInfoWarnEvery.
func (s *PostgresStore) noteTemplatesColumnsMissing(now time.Time, cause error) {
	s.templatesLegacyUntil.Store(now.Add(sysInfoLegacyRetry).UnixNano())
	last := s.templatesWarnedAt.Load()
	if last != 0 && now.UnixNano()-last < int64(sysInfoWarnEvery) {
		return
	}
	if s.templatesWarnedAt.CompareAndSwap(last, now.UnixNano()) {
		fmt.Printf("WARN: Device Templates columns are missing (%v): migration 189 "+
			"(migrate-189-device-templates.sql) has not been applied. Falling back to the "+
			"pre-189 statements: templates rank at the default priority, pilot publishes "+
			"are not polled, and template suggestions are not recorded.\n", cause)
	}
}

// noteTemplatesColumnsPresent announces the recovery once a current
// statement succeeds after a fallback.
func (s *PostgresStore) noteTemplatesColumnsPresent(err error) {
	if err != nil {
		return
	}
	if s.templatesWarnedAt.Swap(0) != 0 {
		fmt.Printf("INFO: Device Templates columns (migration 189) are present again; " +
			"template priorities, pilots and suggestions resumed\n")
	}
}
