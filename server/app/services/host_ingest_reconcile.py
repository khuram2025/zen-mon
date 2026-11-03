"""Inspect and resolve a held analytics batch from the active HA appliance."""
import argparse
import asyncio
import json
import os

from sqlalchemy import text

from app.services.host_ingest_journal import HostIngestJournal, JournalUnavailable


async def run(args):
    from app.core.controller_admission import permitted
    from app.core.database import engine

    if not os.environ.get("ZENPLUS_CONTROLLER_GRANT") or not permitted():
        raise JournalUnavailable("Run this command on the admitted controller")
    try:
        if args.decision:
            await HostIngestJournal(engine, None).reconcile_held(
                args.table, args.batch_id, decision=args.decision, digest=args.digest or "",
                actor=args.actor or "", evidence=args.evidence or "",
            )
            print(json.dumps({"batch_id": args.batch_id, "decision": args.decision}))
        else:
            async with engine.connect() as conn:
                rows = (await conn.execute(text("""
                    SELECT id,table_name,state,hold_reason,attempted_at,
                           octet_length(payload) AS payload_bytes,encode(sha256(payload),'hex') AS payload_sha256
                    FROM host_ingest_batches
                    WHERE state='held' AND (CAST(:table AS text) IS NULL OR table_name=:table)
                    ORDER BY id LIMIT 100
                """), {"table": args.table})).mappings().all()
            print(json.dumps([dict(row) for row in rows], default=str))
    finally:
        await engine.dispose()


def main():
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table")
    parser.add_argument("--batch-id", type=int)
    parser.add_argument("--decision", choices=["confirm_committed", "retry_proven_absent"])
    parser.add_argument("--digest")
    parser.add_argument("--actor")
    parser.add_argument("--evidence")
    args = parser.parse_args()
    if args.decision and not (args.table and args.batch_id and args.batch_id > 0):
        parser.error("A decision requires a table and positive batch ID")
    if os.geteuid() != 0:
        parser.error("Run on the controller as a privileged operator")
    load_dotenv("/opt/zenplus/.env")
    os.environ.setdefault("ZENPLUS_CONTROLLER_GRANT", "/run/zenplus-controller/grant.json")
    try:
        asyncio.run(run(args))
    except (JournalUnavailable, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")
    except Exception as exc:
        parser.exit(1, f"Reconciliation unavailable ({type(exc).__name__})\n")


if __name__ == "__main__":
    main()
