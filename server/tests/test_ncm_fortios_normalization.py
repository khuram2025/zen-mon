"""FortiOS re-encrypts reversible secrets on every print.

Field report (production, November 2026): four FortiGates raised "Running
configuration changed" on every scheduled backup. Each diff was 321-590 lines
added and removed, all of them ``set … ENC <ciphertext>`` lines, and the
redacted diff was empty. These tests pin that an unchanged FortiGate compares
equal, while real changes (including adding/removing a secret or changing an
administrator password hash) still do not.
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid

from app.services.ncm_content import ENCRYPTED_MARKER, config_diff, normalize_config


def fortigate(psk='Xk2Lm9QwErTy1234567890abcdefABCDEF==', user_pw='Zz98YyXxWwVv7766554433==',
              admin_hash='SH2a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0',
              key_body='MIIFHzBJBgkqhkiG9w0BBQ0wPDAbBgkqhkiG9w0BBQwwDgQIabc', extra=''):
    return f'''#config-version=FGT60F-7.2.8-FW-build1639-240313:opmode=0:vdom=0:user=backup
#conf_file_ver=12345678901234
#buildno=1639
#global_vdom=1
config system global
    set hostname "Dammam"
    set timezone 52
end
config system admin
    edit "admin"
        set accprofile "super_admin"
        set password ENC {admin_hash}
    next
end
config user local
    edit "helpdesk"
        set type password
        set passwd ENC {user_pw}
    next
end
config vpn ipsec phase1-interface
    edit "to-HQ"
        set interface "wan1"
        set remote-gw 203.0.113.1
        set psksecret ENC {psk}
    next
end
config vpn certificate local
    edit "Fortinet_Factory"
        set private-key "-----BEGIN ENCRYPTED PRIVATE KEY-----
{key_body}
-----END ENCRYPTED PRIVATE KEY-----"
        set certificate "-----BEGIN CERTIFICATE-----
MIIDdzCCAl+gAwIBAgIEAgAAuTANBgkqhkiG9w0BAQUFADBaMQswCQYDVQQGEwJJ
-----END CERTIFICATE-----"
    next
end
{extra}'''


def test_reencrypted_secrets_alone_are_not_a_change():
    before = fortigate()
    after = fortigate(psk='Qq11Ww22Ee33Rr44Tt55Yy66Uu77Ii88Oo99==', user_pw='Aa00Bb11Cc22Dd33Ee44==',
                      key_body='MIIFHzBJBgkqhkiG9w0BBQ0wPDAbBgkqhkiG9w0BBQwwDgQIxyz')
    assert normalize_config(before) == normalize_config(after)
    summary = config_diff(before, after)
    assert summary['identical'] and summary['added'] == summary['removed'] == 0
    assert not summary['sensitive_changed']
    assert ENCRYPTED_MARKER in normalize_config(before)


def test_setting_name_still_compared_so_adding_a_secret_is_a_change():
    extra = 'config system snmp community\n    edit 1\n        set name ENC Rnd0mCipher==\n    next\nend'
    assert normalize_config(fortigate()) != normalize_config(fortigate(extra=extra))


def test_administrator_password_hash_is_stable_and_still_compared():
    changed = fortigate(admin_hash='SH2ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff')
    summary = config_diff(fortigate(), changed)
    assert not summary['identical'] and summary['sensitive_changed']


def test_real_configuration_change_is_still_detected():
    summary = config_diff(fortigate(), fortigate().replace('203.0.113.1', '203.0.113.9'))
    assert not summary['identical'] and summary['added'] == summary['removed'] == 1


def test_certificates_and_unencrypted_keys_are_still_compared():
    other_cert = fortigate().replace('MIIDdzCCAl', 'MIIDdzCCAm')
    assert normalize_config(fortigate()) != normalize_config(other_cert)
    plain = '-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----'
    assert normalize_config(plain) != normalize_config(plain.replace('AAAA', 'BBBB'))


def test_legacy_pem_encryption_header_is_masked():
    pem = ('-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-256-CBC,{iv}\n\nbody{iv}\n'
           '-----END RSA PRIVATE KEY-----')
    assert normalize_config(pem.format(iv='01')) == normalize_config(pem.format(iv='02'))


def test_non_fortios_configurations_are_unchanged():
    ios = 'hostname sw1\n!\nusername admin secret 9 $9$abc\nntp clock-period 17179\ninterface Gi0/1\n description up'
    assert 'secret 9 $9$abc' in normalize_config(ios)
    assert normalize_config(ios) != normalize_config(ios.replace('$9$abc', '$9$xyz'))


# ── Post-upgrade reconciliation (disposable PostgreSQL) ──────────────────────

from pg_fixture import pg_cluster, pg_db  # noqa: E402,F401  (fixtures)


def _store(db, device, content, *, captured='now()', old_norm=True):
    norm = hashlib.sha256((content if old_norm else normalize_config(content)).encode()).hexdigest()
    return db.scalar(
        "INSERT INTO device_configs (device_id, config_type, content, content_hash, norm_hash, is_change, captured_at)"
        f" VALUES ($1, 'running', $2, $3, $4, true, {captured}) RETURNING id",
        device, content, hashlib.sha256(content.encode()).hexdigest(), norm)


def _alert(db, device, before, after):
    meta = {'ncm': True, 'ncm_event': 'config.changed', 'from_version': str(before), 'to_version': str(after),
            'added': 3, 'removed': 3, 'sensitive_changed': True}
    return db.scalar("INSERT INTO alerts (device_id, status, severity, message, triggered_at, metadata)"
                     " VALUES ($1, 'active', 'warning', 'NCM: Running configuration changed - fgt', now(), $2::jsonb)"
                     " RETURNING id", device, meta)


def test_reconcile_rehashes_and_closes_only_verifiably_false_alerts(pg_db):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.services import ncm_normalization

    device = uuid.uuid4()
    pg_db.execute("INSERT INTO devices (id, hostname, ip_address, device_type, vendor)"
                  " VALUES ($1, 'Dammam', '192.0.2.50', 'firewall', 'Fortinet')", device)
    v1 = _store(pg_db, device, fortigate(), captured="now() - interval '3 days'")
    v2 = _store(pg_db, device, fortigate(psk='Other111Cipher=='), captured="now() - interval '2 days'")
    v3 = _store(pg_db, device, fortigate(psk='Other222Cipher==').replace('203.0.113.1', '203.0.113.7'),
                captured="now() - interval '1 day'")
    false_alert = _alert(pg_db, device, v1, v2)
    real_alert = _alert(pg_db, device, v2, v3)
    pruned_alert = _alert(pg_db, device, uuid.uuid4(), v1)

    async def run():
        engine = create_async_engine(pg_db.url)
        try:
            async with async_sessionmaker(engine)() as db:
                results = []
                while (step := await ncm_normalization.reconcile(db, batch_size=2)) is not None:
                    results.append(step)
                    await db.commit()
                return results
        finally:
            await engine.dispose()

    results = asyncio.run(run())
    assert results[0]['phase'] == 'rehash' and results[-1]['phase'] == 'done'
    assert results[-1]['closed'] == 1 and results[-1]['unverifiable'] == 1

    status = dict(pg_db.query("SELECT id, status FROM alerts"))
    assert status[false_alert] == 'resolved'
    assert status[real_alert] == 'active' and status[pruned_alert] == 'active'
    assert pg_db.scalar("SELECT metadata->>'resolved_by' FROM alerts WHERE id=$1", false_alert) == 'migration'
    assert pg_db.scalar("SELECT count(*) FROM alert_events WHERE alert_id=$1 AND kind='resolved'", false_alert) == 1
    assert pg_db.scalar("SELECT is_change FROM device_configs WHERE id=$1", v2) is False
    # Baseline drift compares norm_hash: v1 and v2 now hash identically.
    hashes = dict(pg_db.query("SELECT id, norm_hash FROM device_configs"))
    assert hashes[v1] == hashes[v2] == hashlib.sha256(normalize_config(fortigate()).encode()).hexdigest()
    assert hashes[v3] != hashes[v1]
    # Done once: a later timer tick is a no-op.
    assert asyncio.run(run()) == []
