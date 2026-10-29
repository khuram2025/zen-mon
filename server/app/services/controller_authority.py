"""Single active controller generation, with mandatory predecessor fencing.

Only the root supervisor's restricted DB identity can use this table. The
application identity has no grants. Database writer election is Patroni's job;
this layer additionally stops an obsolete application executor before takeover.
"""
from uuid import UUID
from sqlalchemy import text

LEASE_SECONDS = 12


class AuthorityUnavailable(RuntimeError):
    pass


async def acquire(db, *, cell, node, boot, token, eligible, fence):
    """Caller commits before admitting work. A failed/uncertain commit admits none.

eligible checks the local commissioned database primary and dependencies.
fence must confirm the previous node is powered off, not merely unreachable.
Holding the row lock serializes candidate fencing and activation transactions.
"""
    await eligible()
    await db.execute(text("INSERT INTO controller_authority(cell_id) VALUES(:cell) ON CONFLICT DO NOTHING"), {'cell': cell})
    row = (await db.execute(text("""SELECT *,lease_expires_at>clock_timestamp() AS live
        FROM controller_authority WHERE cell_id=:cell FOR UPDATE"""), {'cell': cell})).one()
    if row.live:
        raise AuthorityUnavailable('A live controller generation already exists')
    receipt = None
    if row.node_id and row.node_id != node:
        receipt = await fence(row.node_id, str(row.boot_id))
        if (not isinstance(receipt, dict) or receipt.get('node_id') != row.node_id
                or receipt.get('power_state') != 'off' or not receipt.get('request_id')):
            raise AuthorityUnavailable('No verified predecessor power-off receipt')
    # Local restart requires the supervisor to prove all managed units stopped
    # before calling acquire, including when boot identity did not change.
    await eligible()
    generation = row.generation + 1
    args = dict(cell=cell, node=node, boot=UUID(str(boot)), token=UUID(str(token)), generation=generation,
                seconds=LEASE_SECONDS)
    await db.execute(text("""UPDATE controller_authority SET node_id=:node,boot_id=:boot,token=:token,
        generation=:generation,lease_expires_at=clock_timestamp()+make_interval(secs=>:seconds),
        updated_at=clock_timestamp() WHERE cell_id=:cell"""), args)
    import json
    await db.execute(text("""INSERT INTO controller_authority_events
        (cell_id,generation,node_id,prior_node_id,fence_receipt)
        VALUES(:cell,:generation,:node,:prior,CAST(:receipt AS jsonb))"""),
        {**args, 'prior': row.node_id, 'receipt': json.dumps(receipt)})
    return generation


async def renew(db, *, cell, node, token, generation):
    row = (await db.execute(text("""UPDATE controller_authority SET
        lease_expires_at=clock_timestamp()+make_interval(secs=>:seconds),updated_at=clock_timestamp()
        WHERE cell_id=:cell AND node_id=:node AND token=:token AND generation=:generation
          AND lease_expires_at>clock_timestamp() RETURNING generation"""),
        dict(cell=cell, node=node, token=UUID(str(token)), generation=generation, seconds=LEASE_SECONDS))).first()
    if not row:
        raise AuthorityUnavailable('Controller generation expired or replaced')


async def release(db, *, cell, node, token, generation):
    # Keep the predecessor identity: expiry is not evidence its executor died.
    await db.execute(text("""UPDATE controller_authority SET lease_expires_at=clock_timestamp()
        WHERE cell_id=:cell AND node_id=:node AND token=:token AND generation=:generation"""),
        dict(cell=cell, node=node, token=UUID(str(token)), generation=generation))
