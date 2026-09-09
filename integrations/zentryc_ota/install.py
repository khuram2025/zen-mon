"""Install the scoped offer hook. Run against a staged copy before production."""
import argparse
import hashlib
import shutil
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("ota_dir", type=Path)
p.add_argument("--expected-services-sha256", required=True)
p.add_argument("--expected-admin-sha256", required=True)
p.add_argument("--expected-tasks-sha256", required=True)
args = p.parse_args()
source = args.ota_dir / "services.py"
raw = source.read_bytes()
if hashlib.sha256(raw).hexdigest() != args.expected_services_sha256:
    raise SystemExit("OTA services.py changed since review; refusing deployment")
admin_path = args.ota_dir / "views_admin.py"
admin_raw = admin_path.read_bytes()
if hashlib.sha256(admin_raw).hexdigest() != args.expected_admin_sha256:
    raise SystemExit("OTA views_admin.py changed since review; refusing deployment")
tasks_path = args.ota_dir / "tasks.py"
tasks_raw = tasks_path.read_bytes()
if hashlib.sha256(tasks_raw).hexdigest() != args.expected_tasks_sha256:
    raise SystemExit("OTA tasks.py changed since review; refusing deployment")
text = raw.decode().replace("\r\r\n", "\n").replace("\r\n", "\n")
anchor = "    # Channel guard:"
if text.count(anchor) != 1:
    raise SystemExit("Expected offer-selection insertion point is missing or ambiguous")
hook = '''    # ZenPlus follows required bridges; leave the ZenAI policy independent.
    subscription = getattr(appliance, "subscription", None)
    if (getattr(subscription, "subscription_type", "ota") or "ota") == "ota":
        from ota.upgrade_path import get_zenplus_update
        return get_zenplus_update(appliance)

'''
text = text.replace(anchor, hook + anchor, 1)
backup = source.with_name("services.py.pre-upgrade-path")
if backup.exists():
    raise SystemExit("Deployment backup already exists; inspect before rerunning")
backup.write_bytes(raw)
for name in ("upgrade_path.py", "rollout_guard.py", "test_upgrade_path.py"):
    shutil.copy2(Path(__file__).parent / name, args.ota_dir / name)
admin_text = admin_raw.decode().replace("\r\r\n", "\n").replace("\r\n", "\n")
admin_anchor = "    # Parse promote_after"
if admin_text.count(admin_anchor) != 1:
    raise SystemExit("Expected rollout insertion point is missing or ambiguous")
admin_hook = '''    if release.product == "ota":
        from ota.rollout_guard import validate_rollout_path
        try:
            validate_rollout_path(release, data)
        except Exception as exc:
            return Response({"error": f"Upgrade path cannot be promoted: {exc}"}, status=400)

'''
admin_path.with_name("views_admin.py.pre-upgrade-path").write_bytes(admin_raw)
admin_text = admin_text.replace(admin_anchor, admin_hook + admin_anchor, 1)
promote_anchor = '        next_stage, next_pct = next_stages[rollout.stage]'
promote_hook = '''        if rollout.release.product == "ota":
            from ota.rollout_guard import validate_rollout_path
            try:
                validate_rollout_path(rollout.release, {"target_group": rollout.target_group})
            except Exception as exc:
                return Response({"error": f"Upgrade path cannot be promoted: {exc}"}, status=400)
'''
if admin_text.count(promote_anchor) != 1:
    raise SystemExit("Expected promotion insertion point is missing or ambiguous")
admin_text = admin_text.replace(promote_anchor, promote_hook + promote_anchor, 1)
# Preserve the selected audience on manual promotion.
admin_text = admin_text.replace('            stage=next_stage,', '            stage=next_stage,\n            target_group=rollout.target_group,', 1)
tasks_text = tasks_raw.decode().replace("\r\n", "\n")
tasks_anchor = '                        next_stage, next_pct = next_stages[rollout.stage]'
tasks_hook = '''                        if rollout.release.product == "ota":
                            from ota.rollout_guard import validate_rollout_path
                            try:
                                validate_rollout_path(rollout.release, {"target_group": rollout.target_group})
                            except Exception:
                                log_audit("system", "upgrade_path_promotion_blocked", str(rollout.release_id))
                                continue
'''
if tasks_text.count(tasks_anchor) != 1:
    raise SystemExit("Expected automatic promotion insertion point is missing or ambiguous")
tasks_text = tasks_text.replace(tasks_anchor, tasks_hook + tasks_anchor, 1)
tasks_text = tasks_text.replace('                            stage=next_stage,', '                            stage=next_stage,\n                            target_group=rollout.target_group,', 1)
tasks_path.with_name("tasks.py.pre-upgrade-path").write_bytes(tasks_raw)
tasks_path.write_text(tasks_text)
admin_path.write_text(admin_text)
source.write_text(text)
print("Installed scoped ZenPlus upgrade path; previous services.py retained")
