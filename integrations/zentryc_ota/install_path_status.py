"""Install the read-only blocked-path response extension into an existing OTA deployment."""
import argparse
import hashlib
from pathlib import Path
from shutil import copy2

parser = argparse.ArgumentParser()
parser.add_argument("ota", type=Path)
parser.add_argument("--expected-view-sha256", required=True)
args = parser.parse_args()
view = args.ota / "views_appliance.py"
raw = view.read_bytes()
if hashlib.sha256(raw).hexdigest() != args.expected_view_sha256:
    raise SystemExit("Appliance view changed since review")
source = raw.decode()
anchor = '        return Response({"available": False, "release": None})'
replacement = '''        result = {"available": False, "release": None}
        if getattr(appliance, "upgrade_hold", None):
            result["hold"] = appliance.upgrade_hold
        return Response(result)'''
if source.count(anchor) != 1:
    raise SystemExit("Unexpected update check view")
temporary = view.with_suffix(".path-status.tmp")
temporary.write_bytes(source.replace(anchor, replacement).encode())
temporary.chmod(view.stat().st_mode & 0o777)
temporary.replace(view)
copy2(Path(__file__).with_name("check_required_bridges.py"), args.ota / "management/commands")
