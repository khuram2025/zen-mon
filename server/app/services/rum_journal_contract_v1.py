"""Append-only RUM journal operation v1 wire columns and derived query.

Incompatible changes require a new operation kind and a new contract file.
Keep this reader available while v1 journal payloads or receipts exist.
"""

COLUMNS_V1 = ['timestamp',
 'application_id',
 'service_name',
 'env',
 'event_type',
 'session_id',
 'view_id',
 'view_name',
 'url',
 'user_id',
 'country',
 'browser',
 'device_type',
 'lcp',
 'inp',
 'cls',
 'fcp',
 'ttfb',
 'load_ms',
 'error_message',
 'backend_trace_id',
 'attributes',
 'ts_bucket',
 'event_id',
 'sdk_version',
 'service_version',
 'browser_version',
 'os',
 'action_name',
 'action_type',
 'target',
 'duration_ms',
 'resource_url',
 'resource_type',
 'method',
 'status_code',
 'transfer_size',
 'encoded_body_size',
 'error_type',
 'error_stack',
 'error_source',
 'error_fingerprint',
 'end_reason',
 'is_final',
 'sample_rate',
 'vital_attribution',
 'has_lcp',
 'has_inp',
 'has_cls',
 'has_fcp',
 'has_ttfb',
 'has_load',
 'sampled',
 'client_ip',
 'redirect_ms',
 'dns_ms',
 'connect_ms',
 'tls_ms',
 'wait_ms',
 'download_ms',
 'blocked_ms',
 'processing_ms',
 'server_ms',
 'db_ms',
 'has_timing',
 'has_server_timing',
 'protocol',
 'connection_type',
 'connection_rtt_ms',
 'connection_downlink',
 'language',
 'timezone',
 'screen_res',
 'viewport',
 'dedupe_id']

ROLLUP_SQL_V1 = """
INSERT INTO zenplus.apm_rum_metrics_5m
SELECT
    toStartOfFiveMinutes(r.timestamp) AS bucket_timestamp,
    application_id, env, service_version, view_name, browser,
    browser_version, os, device_type, country,
    count() AS events,
    countIf(event_type = 'error') AS errors,
    countIf(event_type = 'error' AND sampled = 1) AS sampled_errors,
    countIf(event_type = 'error' AND sampled = 0) AS unsampled_errors,
    countIf(event_type = 'resource' AND sampled = 1) AS resources,
    countIf(event_type = 'resource' AND sampled = 1
            AND (status_code >= 400 OR attributes['failed'] = 'true')) AS resource_failures,
    countIf(event_type = 'action' AND sampled = 1) AS actions,
    countIf(event_type = 'long_task' AND sampled = 1) AS long_tasks,
    uniqCombined64StateIf(
        concat(application_id, char(31), env, char(31), session_id), sampled = 1
    ) AS sessions,
    uniqCombined64StateIf(
        concat(application_id, char(31), env, char(31), session_id),
        event_type = 'error' AND sampled = 1
    ) AS error_sessions,
    uniqCombined64StateIf(
        concat(application_id, char(31), env, char(31), view_id),
        sampled = 1 AND event_type = 'view'
            AND (sdk_version = '' OR (is_final = 0 AND end_reason = 'view_start'))
    ) AS views,
    quantileTDigestStateIf(0.75)(r.lcp, sampled = 1 AND event_type = 'view'
        AND (is_final = 1 OR sdk_version = '')
        AND (has_lcp = 1 OR (sdk_version = '' AND r.lcp > 0))) AS lcp,
    quantileTDigestStateIf(0.75)(r.inp, sampled = 1 AND event_type = 'view'
        AND (is_final = 1 OR sdk_version = '')
        AND (has_inp = 1 OR (sdk_version = '' AND r.inp > 0))) AS inp,
    quantileTDigestStateIf(0.75)(r.cls, sampled = 1 AND event_type = 'view'
        AND (is_final = 1 OR sdk_version = '')
        AND (has_cls = 1 OR (sdk_version = '' AND r.cls > 0))) AS cls,
    quantileTDigestStateIf(0.75)(r.fcp, sampled = 1 AND event_type = 'view'
        AND (is_final = 1 OR sdk_version = '')
        AND (has_fcp = 1 OR (sdk_version = '' AND r.fcp > 0))) AS fcp,
    quantileTDigestStateIf(0.75)(r.ttfb, sampled = 1 AND event_type = 'view'
        AND (is_final = 1 OR sdk_version = '')
        AND (has_ttfb = 1 OR (sdk_version = '' AND r.ttfb > 0))) AS ttfb,
    quantileTDigestStateIf(0.75)(r.load_ms, sampled = 1 AND event_type = 'view'
        AND (is_final = 1 OR sdk_version = '')
        AND (has_load = 1 OR (sdk_version = '' AND r.load_ms > 0))) AS load_ms,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '')
            AND (has_lcp = 1 OR (sdk_version = '' AND r.lcp > 0))) AS lcp_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '')
            AND (has_inp = 1 OR (sdk_version = '' AND r.inp > 0))) AS inp_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '')
            AND (has_cls = 1 OR (sdk_version = '' AND r.cls > 0))) AS cls_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '')
            AND (has_fcp = 1 OR (sdk_version = '' AND r.fcp > 0))) AS fcp_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '')
            AND (has_ttfb = 1 OR (sdk_version = '' AND r.ttfb > 0))) AS ttfb_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '')
            AND (has_load = 1 OR (sdk_version = '' AND r.load_ms > 0))) AS load_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_lcp = 1 OR (sdk_version = '' AND r.lcp > 0)) AND r.lcp <= 2500) AS lcp_good,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_lcp = 1 OR (sdk_version = '' AND r.lcp > 0)) AND r.lcp > 4000) AS lcp_poor,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_inp = 1 OR (sdk_version = '' AND r.inp > 0)) AND r.inp <= 200) AS inp_good,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_inp = 1 OR (sdk_version = '' AND r.inp > 0)) AND r.inp > 500) AS inp_poor,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_cls = 1 OR (sdk_version = '' AND r.cls > 0)) AND r.cls <= 0.1) AS cls_good,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_cls = 1 OR (sdk_version = '' AND r.cls > 0)) AND r.cls > 0.25) AS cls_poor,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_fcp = 1 OR (sdk_version = '' AND r.fcp > 0)) AND r.fcp <= 1800) AS fcp_good,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_fcp = 1 OR (sdk_version = '' AND r.fcp > 0)) AND r.fcp > 3000) AS fcp_poor,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_ttfb = 1 OR (sdk_version = '' AND r.ttfb > 0)) AND r.ttfb <= 800) AS ttfb_good,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_ttfb = 1 OR (sdk_version = '' AND r.ttfb > 0)) AND r.ttfb > 1800) AS ttfb_poor,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_load = 1 OR (sdk_version = '' AND r.load_ms > 0)) AND r.load_ms <= 2500) AS load_good,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_load = 1 OR (sdk_version = '' AND r.load_ms > 0)) AND r.load_ms > 4000) AS load_poor,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_lcp = 1 OR (sdk_version = '' AND r.lcp > 0))) AS lcp_rated,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_inp = 1 OR (sdk_version = '' AND r.inp > 0))) AS inp_rated,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_cls = 1 OR (sdk_version = '' AND r.cls > 0))) AS cls_rated,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_fcp = 1 OR (sdk_version = '' AND r.fcp > 0))) AS fcp_rated,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_ttfb = 1 OR (sdk_version = '' AND r.ttfb > 0))) AS ttfb_rated,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_load = 1 OR (sdk_version = '' AND r.load_ms > 0))) AS load_rated
FROM zenplus.apm_rum_events AS r
WHERE r.timestamp >= fromUnixTimestamp64Milli({batch_from_ms:Int64})
  AND r.timestamp <= fromUnixTimestamp64Milli({batch_to_ms:Int64})
  AND dedupe_id IN {dedupe_ids:Array(String)}
GROUP BY bucket_timestamp, application_id, env, service_version, view_name,
         browser, browser_version, os, device_type, country
"""
