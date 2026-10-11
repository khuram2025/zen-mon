"""Real PostgreSQL UDT regressions on an explicitly disposable database."""

import asyncio, os, json, time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession


def test_managed_ports_gateway_evidence_and_history():
    dsn = os.environ.get("UDT_TEST_DSN")
    if not dsn:
        pytest.skip("UDT_TEST_DSN not set")
    target = urlparse(dsn)
    assert (
        target.hostname in ("127.0.0.1", "localhost") and target.path == "/udt_fixture"
    ), "Use the named disposable UDT database"

    async def scenario():
        import asyncpg
        from app.services.udt_gateway_service import save_inventory, save_macs, save_arp
        from app.api.v1.udt import (
            capacity,
            device_ports,
            list_ports,
            list_endpoints,
            udt_summary,
            _require_direct_switch,
        )
        from app.services.udt_ad_service import _record_events
        from app.services.udt_sweeper import _close_stale

        root = Path(__file__).resolve().parents[2]
        schema = "fixture_" + uuid4().hex
        setup = await asyncpg.connect(dsn)
        await setup.execute(f"CREATE SCHEMA {schema}; SET search_path TO {schema};")
        try:
            await setup.execute(
                """CREATE TABLE devices(id uuid PRIMARY KEY,hostname varchar(255),location varchar(255),device_type text,ip_address inet);
    CREATE TABLE device_interfaces(device_id uuid,if_index int,if_name varchar(255),if_descr varchar(255),if_alias varchar(255),if_type integer,admin_status varchar(20),oper_status varchar(20),if_speed bigint,mac_address macaddr,PRIMARY KEY(device_id,if_index));
    CREATE TABLE snmp_credentials(id uuid PRIMARY KEY); CREATE TABLE ncm_credentials(id uuid PRIMARY KEY); CREATE TABLE users(id uuid PRIMARY KEY); CREATE TABLE windows_credentials(id uuid PRIMARY KEY);
    CREATE TABLE alert_rules(metric text); CREATE TABLE roles(name text,permissions jsonb);
    INSERT INTO roles VALUES('admin','["*"]');"""
            )
            await setup.execute((root / "scripts/migrate-060-udt.sql").read_text())
            await setup.execute(
                (root / "scripts/migrate-065-udt-device-settings.sql").read_text()
            )
            await setup.execute(
                (root / "scripts/migrate-119-udt-ip-evidence.sql").read_text()
            )
            await setup.execute(
                "ALTER TABLE udt_endpoints ADD COLUMN type_source text DEFAULT 'auto';"
            )
            await setup.execute(
                (root / "scripts/migrate-160-udt-managed-switches.sql").read_text()
            )
            # Migration replay must leave history and identities intact.
            await setup.execute(
                (root / "scripts/migrate-160-udt-managed-switches.sql").read_text()
            )
            engine = create_async_engine(
                dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
                connect_args={"server_settings": {"search_path": schema}},
            )
            async with AsyncSession(engine, expire_on_commit=False) as db:
                cid = str(uuid4())
                other = str(uuid4())
                await db.execute(
                    text(
                        "INSERT INTO devices VALUES(:id,'Controller','Lab','firewall','192.0.2.254'),(:other,'Router','Lab','router','192.0.2.253')"
                    ),
                    {"id": cid, "other": other},
                )
                await db.execute(
                    text(
                        "INSERT INTO device_interfaces VALUES(:id,700,'internal','internal',NULL,6,'up','up',1000000000,NULL),(:other,900,'internal','internal',NULL,6,'up','up',1000000000,NULL)"
                    ),
                    {"id": cid, "other": other},
                )
                switches = [
                    {
                        "serial": f"SAMPLE{i:04}",
                        "hostname": f"Switch {i:02}",
                        "instance": f"1.9.{23000000+i}",
                        "status": "up",
                    }
                    for i in range(33)
                ]
                ports = [
                    {
                        "serial": s["serial"],
                        "port_number": i,
                        "if_name": f"port{i}",
                        "oper_status": "up" if i < 12 else "down",
                        "pvid": 24,
                    }
                    for s in switches
                    for i in range(1, 51)
                ]
                ports += [
                    {
                        "serial": switches[0]["serial"],
                        "port_number": i,
                        "if_name": f"port{i}",
                        "oper_status": "down",
                        "pvid": 24,
                    }
                    for i in range(51, 66)
                ]
                ids = await save_inventory(db, cid, switches, ports)
                assert len(ids) == 33
                again = await save_inventory(db, cid, switches, ports)
                assert ids == again
                macs = [
                    {
                        "serial": switches[0]["serial"],
                        "port_number": 4,
                        "vlan_id": 24,
                        "mac": "aa:bb:cc:dd:ee:02",
                        "is_direct": True,
                    },
                    {
                        "serial": switches[1]["serial"],
                        "port_number": 4,
                        "vlan_id": 24,
                        "mac": "aa:bb:cc:dd:ee:04",
                        "is_direct": True,
                    },
                ]
                assert await save_macs(db, ids, ports, macs) == 2
                assert await save_macs(db, ids, ports, macs) == 2
                assert (
                    await db.execute(text("SELECT count(*) FROM udt_managed_locations"))
                ).scalar() == 2
                arp = [
                    {
                        "ip": "192.0.2.10",
                        "mac": "aa:bb:cc:dd:ee:02",
                        "interface": "internal",
                    }
                ]
                assert await save_arp(db, cid, arp) == 1
                assert (
                    await save_arp(db, cid, [arp[0] | {"interface": "unmapped"}]) == 0
                )
                await db.commit()
                user = SimpleNamespace(role="admin")
                cap = await capacity(db, user)
                assert (
                    len(cap["data"]) == 33
                    and sum(x["total"] for x in cap["data"]) == 1665
                )
                row = await device_ports(ids[switches[0]["serial"]], True, db, user)
                assert (
                    row["device"]["managed"]
                    and row["device"]["controller_id"] == cid
                    and row["ports"][3]["active_endpoints"] == 1
                )
                global_ports = await list_ports(None, False, False, 0, 1000, db, user)
                assert global_ports["meta"]["total"] == 1665
                summary = await udt_summary(db, user)
                assert summary["switches"] == 33 and summary["total_ports"] == 1665
                kwargs = dict(
                    q=None,
                    status=None,
                    endpoint_type=None,
                    vlan=None,
                    device_id=None,
                    flag=None,
                    include_ignored=False,
                    sort="last_seen",
                    order="desc",
                    skip=0,
                    limit=50,
                    db=db,
                    user=user,
                )
                endpoints = await list_endpoints(**kwargs)
                e = next(e for e in endpoints["data"] if e["mac"] == arp[0]["mac"])
                assert (
                    e["ip"] == "192.0.2.10"
                    and e["switch_hostname"] == "Switch 00"
                    and e["if_name"] == "port4"
                    and e["gateway_hostname"] == "Controller"
                )
                assert (
                    await db.execute(text("SELECT count(*) FROM devices"))
                ).scalar() == 2
                from fastapi import HTTPException

                with pytest.raises(HTTPException) as error:
                    await _require_direct_switch(db, ids[switches[0]["serial"]])
                assert error.value.status_code == 409
                # Retain expired periods; real re-observation appends a period.
                await db.execute(
                    text(
                        "UPDATE udt_ip_history SET active=false,last_seen=NOW()-INTERVAL '2 days',first_seen=NOW()-INTERVAL '3 days'"
                    )
                )
                await save_arp(db, cid, arp)
                assert (
                    await db.execute(text("SELECT count(*) FROM udt_ip_history"))
                ).scalar() == 2
                await db.commit()
                # Cross-reporter/shared addresses remain separate evidence, never churn.
                shared = arp[0] | {"mac": "aa:bb:cc:dd:ee:04"}
                await save_arp(db, other, [shared])
                await db.commit()
                await save_arp(db, cid, arp)
                await db.commit()
                assert (
                    await db.execute(text("SELECT count(*) FROM udt_ip_evidence"))
                ).scalar() == 2
                # An ambiguous login stays recorded but unassigned.
                dc = str(uuid4())
                await db.execute(
                    text(
                        "INSERT INTO udt_domain_controllers(id,name,host) VALUES(:id,'DC','dc.example.test')"
                    ),
                    {"id": dc},
                )
                ts = datetime.now(timezone.utc)
                count, cursor = await _record_events(
                    db, {"id": dc}, [{"Time": ts.isoformat(), "Cursor": True}]
                )
                assert count == 0 and cursor == ts
                assert (
                    await db.execute(text("SELECT count(*) FROM udt_user_logins"))
                ).scalar() == 0
                await _record_events(
                    db,
                    {"id": dc},
                    [
                        {
                            "Time": ts.isoformat(),
                            "User": "sample.user",
                            "Ip": "192.0.2.10",
                            "Id": 4624,
                            "LogonType": 3,
                        }
                    ],
                )
                assert (
                    await db.execute(text("SELECT endpoint_id FROM udt_user_logins"))
                ).scalar() is None
                # Collection gaps expire attachment state while preserving every period.
                await db.execute(
                    text(
                        "UPDATE udt_managed_locations SET last_seen=NOW()-INTERVAL '20 minutes'"
                    )
                )
                await _close_stale(db)
                assert (
                    await db.execute(text("SELECT count(*) FROM udt_managed_locations"))
                ).scalar() == 2
                assert (
                    await db.execute(
                        text("SELECT count(*) FROM udt_managed_locations WHERE active")
                    )
                ).scalar() == 0
                # Production-sized synthetic inventory stays responsive. These
                # inserts exist only inside the named disposable fixture schema.
                await db.execute(
                    text(
                        """INSERT INTO udt_endpoints(mac,ip_address,last_seen)
                  SELECT ('02:00:00:'||lpad(to_hex((n/65536)%256),2,'0')||':'||lpad(to_hex((n/256)%256),2,'0')||':'||lpad(to_hex(n%256),2,'0'))::macaddr,
                  '198.18.0.0'::inet+n,NOW() FROM generate_series(1,16654) n"""
                    )
                )
                await db.execute(
                    text(
                        """INSERT INTO udt_managed_locations(endpoint_id,switch_id,port_number,vlan_id,is_direct)
                  SELECT id,CAST(:sid AS uuid),1+(row_number() OVER(ORDER BY mac)%48),24,FALSE FROM udt_endpoints WHERE mac::text LIKE '02:00:00:%'"""
                    ),
                    {"sid": ids[switches[0]["serial"]]},
                )
                await db.execute(text("ANALYZE udt_endpoints"))
                await db.execute(text("ANALYZE udt_managed_locations"))
                started = time.monotonic()
                loaded_cap = await capacity(db, user)
                capacity_s = time.monotonic() - started
                started = time.monotonic()
                loaded_endpoints = await list_endpoints(**kwargs)
                endpoints_s = time.monotonic() - started
                assert loaded_endpoints["meta"]["total"] == 16656
                assert len(loaded_cap["data"]) == 33
                # Catch accidental full cross-products while allowing slow CI hosts.
                assert capacity_s < 10 and endpoints_s < 10
                print(
                    json.dumps(
                        {
                            "fixture_endpoints": 16656,
                            "fixture_ports": 1665,
                            "capacity_s": round(capacity_s, 3),
                            "endpoint_page_s": round(endpoints_s, 3),
                        }
                    )
                )
                await db.rollback()
            await engine.dispose()
        finally:
            await setup.execute("ROLLBACK")
            await setup.execute(f"DROP SCHEMA {schema} CASCADE")
            await setup.close()

    asyncio.run(scenario())
