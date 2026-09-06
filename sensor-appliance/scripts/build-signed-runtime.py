#!/usr/bin/env python3
"""Build a sensor release using an explicitly selected Ed25519 signing key.

The private key stays on the release host. Use the same public key for the
controller's ZENPLUS_SENSOR_RELEASE_PUBLIC_KEY and the appliance binary.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--key', required=True, type=Path)
p.add_argument('--version', required=True)
p.add_argument('--output', required=True, type=Path)
p.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2] / 'poller')
args = p.parse_args()
key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
if not isinstance(key, Ed25519PrivateKey):
    p.error('An Ed25519 private key is required')
public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode().strip()
args.output.mkdir(parents=True, exist_ok=True)
binary = args.output / 'zenplus-sensor'
flags = '-s -w -X main.version=' + args.version + " -X 'main.releasePublicKeyPEM=" + public + "'"
subprocess.run([shutil.which('go') or '/usr/local/go/bin/go', 'build', '-trimpath', '-ldflags', flags,
                '-o', str(binary.resolve()), './cmd/sensor'], cwd=args.source, env={**os.environ, 'GOOS': 'linux', 'GOARCH': 'amd64'}, check=True)
digest = hashlib.sha256(binary.read_bytes()).hexdigest()
manifest = json.dumps({'product': 'ZenPlus Remote Sensor', 'platform': 'linux-amd64', 'os': 'linux', 'arch': 'amd64',
                      'version': args.version, 'binary': 'zenplus-sensor', 'binary_url': 'zenplus-sensor',
                      'sha256': digest}, sort_keys=True, separators=(',', ':')).encode()
(args.output / 'manifest.json').write_bytes(manifest)
(args.output / 'manifest.json.sig').write_bytes(key.sign(manifest))
(args.output / 'zenplus-sensor.sha256').write_text(digest + '  zenplus-sensor\n')
(args.output / 'release.pub').write_text(public + '\n')
print('Built and signed', args.version, digest)
