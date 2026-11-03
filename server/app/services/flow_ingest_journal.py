"""Adapt the native collector wire types to the HTTP analytics driver."""
from ipaddress import IPv4Address

TABLES = frozenset({'flow_records', 'flow_collector_stats', 'flow_templates',
                    'flow_app_names', 'flow_if_counters'})
IPV4_COLUMNS = frozenset({'exporter_ip', 'src_addr', 'dst_addr', 'next_hop'})


def write(client_factory, table, columns, rows, token):
    from app.services.host_insert_batcher import HostInsertBatcher
    if table not in TABLES:
        raise ValueError('Unsupported flow journal target')
    # The Go native driver takes network-order UInt32 for IPv4. The Python
    # HTTP driver's binary codec takes a string or IPv4Address. Conversion is
    # deterministic and keeps the persisted payload and original token intact.
    indices = [i for i, name in enumerate(columns) if name in IPV4_COLUMNS]
    adapted = []
    for row in rows:
        if len(row) != len(columns):
            raise ValueError('Invalid flow journal row width')
        values = list(row)
        for i in indices:
            if isinstance(values[i], int):
                if isinstance(values[i], bool):
                    raise ValueError('Invalid flow journal IPv4 value')
                values[i] = str(IPv4Address(values[i]))
        adapted.append(values)
    return HostInsertBatcher(client_factory)._write(table, columns, adapted, token)
