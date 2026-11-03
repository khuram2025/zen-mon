"""Fresh, cell-local ClickHouse replicas and Keeper voters.

This renderer does not adopt, delete or convert an installed data directory.
Inputs and rendered XML containing secrets belong outside the release tree.
Application protocols bind loopback; only authenticated TLS replication leaves
the host. A separate replicated database supplies DDL coordination.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from database_profile import validate

TLS = '/etc/zenplus-ha/analytics-tls'
ROOT = '/var/lib/zenplus-ha/analytics'


def element(parent, name, value=None):
    child = ET.SubElement(parent, name)
    if value is not None:
        child.text = str(value)
    return child


def tls_config(root):
    ssl = element(root, 'openSSL')
    for kind in ('server', 'client'):
        node = element(ssl, kind)
        for key, value in {
            'certificateFile': TLS+'/node.crt', 'privateKeyFile': TLS+'/node.key',
            'caConfig': TLS+'/ca.crt', 'verificationMode': 'strict',
            'loadDefaultCAFile': 'false', 'disableProtocols': 'sslv2,sslv3,tlsv1,tlsv1_1',
            'extendedVerification': 'true', 'cacheSessions': 'true',
        }.items():
            element(node, key, value)
        element(element(node, 'invalidCertificateHandler'), 'name', 'RejectCertificateHandler')


def xml(root):
    ET.indent(root)
    return ET.tostring(root, encoding='unicode')+'\n'


def render(inventory, node_name, credentials):
    validate(inventory)
    if set(credentials) != {'application', 'administration', 'replication', 'keeper'}:
        raise ValueError('Four distinct analytics credentials are required')
    if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', v)
           for v in credentials.values()) or len(set(credentials.values())) != 4:
        raise ValueError('Analytics credentials must be distinct 32–128 character tokens')
    node = next((n for n in inventory['nodes'] if n['name'] == node_name), None)
    if node is None:
        raise ValueError('Unknown analytics member')
    keeper = ET.Element('clickhouse')
    element(keeper, 'path', ROOT+'/keeper/')
    element(keeper, 'listen_host', node['address'])
    element(element(keeper, 'logger'), 'level', 'warning')
    element(keeper.find('logger'), 'console', 1)
    ks = element(keeper, 'keeper_server')
    element(ks, 'storage_path', ROOT+'/keeper')
    element(ks, 'tcp_port_secure', 9281)
    element(ks, 'server_id', inventory['nodes'].index(node)+1)
    element(ks, 'log_storage_path', ROOT+'/keeper/log')
    element(ks, 'snapshot_storage_path', ROOT+'/keeper/snapshots')
    element(ks, 'max_memory_usage_soft_limit', 384*1024*1024)
    element(ks, 'four_letter_word_white_list', 'ruok,mntr,srvr')
    coordination = element(ks, 'coordination_settings')
    for key, value in {'operation_timeout_ms': 10000, 'session_timeout_ms': 30000,
                       'raft_logs_level': 'warning', 'force_sync': 'true', 'quorum_reads': 'true',
                       'snapshots_to_keep': 3, 'reserved_log_items': 10000}.items():
        element(coordination, key, value)
    raft = element(ks, 'raft_configuration')
    element(raft, 'secure', 'true')
    for index, member in enumerate(inventory['nodes'], 1):
        peer = element(raft, 'server')
        for key, value in {'id': index, 'hostname': member['address'], 'port': 9444}.items():
            element(peer, key, value)
    tls_config(keeper)
    result = {'keeper.xml': xml(keeper)}
    if not node['data']:
        return result
    server = ET.Element('clickhouse')
    values = {
        'listen_host': '127.0.0.1', 'http_port': 8123, 'tcp_port': 9000,
        'interserver_listen_host': node['address'], 'interserver_https_port': 9010,
        'interserver_https_host': node['address'], 'path': ROOT+'/data/',
        'tmp_path': ROOT+'/data/tmp/', 'user_files_path': ROOT+'/data/user_files/',
        'format_schema_path': ROOT+'/data/format_schemas/',
        'max_server_memory_usage': 1024*1024*1024, 'background_schedule_pool_size': 16,
        'background_pool_size': 16, 'background_fetches_pool_size': 4,
        'background_buffer_flush_schedule_pool_size': 2,
        'background_message_broker_schedule_pool_size': 2,
        'background_distributed_schedule_pool_size': 2,
        'default_replica_path': '/zenplus-ha/'+inventory['cluster']+'/tables/{shard}/{uuid}',
        'default_replica_name': '{replica}',
    }
    for key, value in values.items():
        element(server, key, value)
    element(element(server, 'logger'), 'level', 'warning')
    element(server.find('logger'), 'console', 1)
    macros = element(server, 'macros')
    element(macros, 'shard', '1'); element(macros, 'replica', node_name)
    transport = element(server, 'interserver_http_credentials')
    element(transport, 'user', 'ha_replication')
    element(transport, 'password', credentials['replication'])
    element(transport, 'allow_empty', 'false')
    zk = element(server, 'zookeeper')
    for member in inventory['nodes']:
        peer = element(zk, 'node')
        element(peer, 'host', member['address']); element(peer, 'port', 9281)
        element(peer, 'secure', 1)
    element(zk, 'identity', 'zenplus:'+credentials['keeper'])
    element(zk, 'session_timeout_ms', 30000)
    profiles = element(server, 'profiles')
    for name in ('default', 'ha_application', 'ha_administration'):
        profile = element(profiles, name)
        for key, value in {'max_threads': 2, 'max_memory_usage': 256*1024*1024,
                           'insert_quorum': 2, 'insert_quorum_timeout': 10000,
                           'insert_quorum_parallel': 1, 'async_insert': 0,
                           'wait_for_async_insert': 1,
                           'deduplicate_blocks_in_dependent_materialized_views': 1}.items():
            element(profile, key, value)
        if name == 'ha_application':
            constraints = element(profile, 'constraints')
            for key, value in {'insert_quorum': 2, 'async_insert': 0}.items():
                entry = element(constraints, key)
                element(entry, 'min', value); element(entry, 'max', value)
    users = element(server, 'users')
    for name, credential, profile in [('default', 'application', 'ha_application'),
                                       ('ha_admin', 'administration', 'ha_administration')]:
        user = element(users, name)
        element(user, 'password_sha256_hex', hashlib.sha256(credentials[credential].encode()).hexdigest())
        element(element(user, 'networks'), 'ip', '127.0.0.1')
        element(user, 'profile', profile); element(user, 'quota', 'default')
        element(user, 'access_management', 0)
    element(element(server, 'quotas'), 'default')
    # Replicated parts must also reach durable media before acceptance.
    mt = element(server, 'merge_tree')
    element(mt, 'fsync_after_insert', 1); element(mt, 'fsync_part_directory', 1)
    # Insert flushing does not cover background merges or replica fetches.
    # A whole-cell power loss must not leave every copy of a merged part empty.
    element(mt, 'min_compressed_bytes_to_fsync_after_merge', 1)
    element(mt, 'min_compressed_bytes_to_fsync_after_fetch', 1)
    element(mt, 'replicated_deduplication_window', 10000)
    tls_config(server)
    result['clickhouse.xml'] = xml(server)
    client = ET.Element('config')
    for key, value in {'host': '127.0.0.1', 'port': 9000, 'user': 'ha_admin',
                       'password': credentials['administration']}.items():
        element(client, key, value)
    result['client.xml'] = xml(client)
    result['database.sql'] = ("CREATE DATABASE zenplus ENGINE = Replicated('/zenplus-ha/"+
        inventory['cluster']+"/database', '1', '"+node_name+"');\n")
    return result


def write_new(output, files):
    output=Path(output)
    output.mkdir(mode=0o700,parents=False,exist_ok=False)
    for name,body in files.items():
        fd=os.open(output/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
            stream.write(body)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('inventory','node','credentials','output'):
        parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    if os.name!='posix':
        parser.error('Render on Linux to enforce private file permissions')
    source=Path(args.credentials)
    if source.is_symlink() or not source.is_file() or source.stat().st_mode & 0o077:
        parser.error('Credentials must be an owner-only regular JSON file')
    files=render(json.loads(Path(args.inventory).read_text()),args.node,json.loads(source.read_text()))
    write_new(args.output,files)


if __name__=='__main__':
    main()
