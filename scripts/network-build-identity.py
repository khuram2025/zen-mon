"""Read-only source inventory for comparing development and installed code.

Text hashes normalize CRLF. No credential/config files are read. This does not
attest which binary a running service has loaded.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--compare', type=Path)
args = parser.parse_args()
root = args.root.resolve()
files = {}
for folder, extensions in [('server/app', {'.py'}), ('poller/internal', {'.go'}),
                           ('dashboard/src', {'.ts', '.tsx', '.css'}), ('scripts', {'.sql'})]:
    for path in sorted((root/folder).rglob('*')):
        if path.is_file() and path.suffix in extensions and not path.name.endswith('_test.go'):
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_text(encoding='utf-8-sig').encode()).hexdigest()
try:
    head = subprocess.run(['git','rev-parse','HEAD'], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
except (OSError, subprocess.CalledProcessError):
    head = None
report = {'format_version':1, 'git_head':head, 'source_files':files,
          'source_digest':hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
          'runtime_binary_attested':False}
if args.compare:
    prior = json.loads(args.compare.read_text(encoding='utf-8'))['source_files']
    report['comparison'] = {'missing':sorted(set(prior)-set(files)), 'added':sorted(set(files)-set(prior)),
                            'changed':sorted(k for k in files.keys() & prior.keys() if files[k] != prior[k])}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps({'git_head':head, 'source_digest':report['source_digest'], 'file_count':len(files),
                  'comparison':report.get('comparison')}, indent=2))
