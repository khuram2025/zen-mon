# ZenPlus Release Path Runbook

How a change gets from the development machine (.221) to customer appliances, which
machine holds which secret, and how the updater decides what to trust. Where this
document and `15-RELEASE-RUNBOOK.md` disagree (key location, build host), this one is
current.

## 1. Who holds what

| Environment | Holds | Never holds |
|---|---|---|
| Development (.221) | Source, Git, toolchains, simulator labs, the **test** signing key | Production signing key, zentryc.com admin login |
| Build | A clean clone of one pushed tag, pinned toolchain | Uncommitted changes, any production secret |
| Signing and publishing | Production signing key, publishing login (MFA on) | Development tools, source edits |
| Lab appliances | Trust for the test key only | Production trust |
| Reference appliance | Production trust; enrolled in the canary group | Development tools, the test key |
| Customer appliances | Production trust only | Any key that can sign |

A customer appliance is never a copy of .221's disk. It runs the same signed release
that was built from a tag and tested on lab and reference appliances.

## 2. Cutting a release

1. **Tag and push.** Merge to the release branch, `git tag v<version>`, push the branch
   and the tag to GitHub. Released source must exist off the development machine.
2. **Clean clone.** `git clone --branch v<version> <repo> /home/zen/release-<version>`.
   Check `ls -d /home/zen/*<version>*` and `pgrep -af "build-release|build-ota"` first:
   never reuse a version another session has started.
3. **Build** from that clone (not from `/opt/zenplus`), signing with the **test** key:

   ```bash
   ZENPLUS_DIR=/home/zen/release-<version> \
   ZENPLUS_RELEASE_PRIVATE_KEY=<test private key> \
   python3 scripts/build-release.py build --version <version> --severity security \
       --require-tag --skip-agent-artifacts
   ```

   (Drop `--skip-agent-artifacts` for a release that also ships a Windows agent.) The
   builder refuses a working tree with uncommitted changes, or a directory that is not a
   git checkout, records `source.commit`, `source.branch`, `source.tag` and
   `source.dirty` in the signed manifest, and signs the SHA-256 of **every file** in the
   package (`payload_files`). `--allow-dirty` exists for lab builds only; the offline
   verifier and the re-signing tool both reject such a package.
4. **Verify offline** before any production API call:

   ```bash
   python3 scripts/verify-ota-release.py update-<version>.zup <public key> \
       --version <version> --appliance-only
   ```

   It fails if the manifest has no signed inventory, if the archive contains a file
   that is not in it (or lacks one that is), if any file differs from its signed hash,
   or if the recorded source is dirty or has no commit.
5. **Lab test.** Fresh install, and upgrades from the oldest supported version, on lab
   appliances with the test key.
6. **Production signing.** Copy the lab-tested `.zup` to the signing machine and re-sign
   it. The tool verifies the package completely (test signature, signed inventory, every
   file hash, clean recorded source) and writes a copy whose manifest is byte-for-byte
   unchanged with only the signature replaced, so customers receive exactly what was
   tested:

   ```bash
   python3 scripts/resign-release.py update-<version>.zup \
       --test-public-key <test.pub> --signing-key <production private key> \
       --output final/update-<version>.zup
   python3 scripts/verify-ota-release.py final/update-<version>.zup \
       <production public key> --version <version> --appliance-only
   ```

7. **Canary, then everyone.**

   ```bash
   python3 scripts/build-release.py publish --file final/update-<version>.zup --version <version> \
       --severity security --skip-agent-artifacts --rollout canary --rollout-group canary
   # reference appliance updates through the real OTA path; check it, then:
   python3 scripts/build-release.py rollout --version <version> --stage full
   ```

   Rollouts auto-promote after 24 hours while failures stay under 5%.
8. **New installs.** Build the customer image from the same signed release on a fresh
   golden VM (`scripts/provision-main-appliance-golden.sh`). Never run
   `prepare-main-appliance-ova.sh` on .221.

## 3. What the appliance checks before it installs anything

The updater runs as root, so it authenticates a package **before writing any of it to
disk** (`updater/package_trust.py`):

1. The archive may contain only plain files and directories with safe relative names.
   Links, device files, absolute or `..` paths, duplicates and oversized entries are
   refused.
2. `manifest.json` and its signature are read into memory and verified against the
   release keys; the manifest must not be older than `max_manifest_age_days`, and its
   version must be the one the server offered.
3. The signed manifest must carry `payload_files`, naming exactly the files in the
   archive.
4. Files are then written one at a time into a root-only directory
   (`/var/lib/zenplus-updater/work`), each hashed as it is written and compared with
   the signed value.

## 4. Updater trust settings

Everything the root updater trusts comes from places the `zenplus` service account
cannot write. `updater/config/agent.conf` is **not** consulted for any of it; a
differing value there is ignored and logged.

| Setting | Where |
|---|---|
| Server URL (must be `https://`), TLS verification, manifest age, legacy-package policy, revoked keys | `/etc/zenplus/updater-trust.conf` (root, 0644) |
| Default release key | `/opt/zenplus/updater/keys/zentryc-release.pub` |
| Additional release keys | `/etc/zenplus/updater-keys.d/*.pub` |

If any of those files, or any directory above them, is not owned by root or is writable
by group or others, the updater **refuses to run** and says why in the journal
(`journalctl -u zenplus-updater`). `scripts/setup-updater-trust.sh` (run on every OTA
update and by `install.sh`) creates the files with safe ownership, hands the updater
code, `scripts/`, `bin/` and the Python virtualenv to root, and removes the service
account from the `docker` group. It never overwrites an administrator's trust file.

### Legacy packages

Packages built before signed inventories (up to 1.23.31) are refused by default. An
air-gapped site that must install one sets `allow_legacy_packages = true` in
`/etc/zenplus/updater-trust.conf`, installs, and turns it back off.

### Rotating or revoking a release key

1. Generate the new keypair on the signing machine.
2. Ship the new public key to appliances in a release signed by the **old** key (a hook
   that copies it to `/etc/zenplus/updater-keys.d/`).
3. Once appliances trust it, sign releases with the new key.
4. To stop trusting a key, add its fingerprint to `revoked_key_fingerprints` in the
   trust file (via a hook in a release signed by the new key):

   ```bash
   python3 -c "from updater.crypto import load_public_key, public_key_fingerprint; \
   print(public_key_fingerprint(load_public_key('old.pub')))"
   ```

## 5. Transition to this model

- Packages up to 1.23.31 are installed by an updater that still unpacks before it
  verifies. The first release built with this tooling is installed by that old updater,
  so the exposure closes only when that release has been installed. Publish it as
  `--severity security` and roll out promptly.
- The production key and the zentryc.com login have been readable by every process
  running as `zen` on .221. Move both to the signing machine, turn on MFA for the
  portal account, and rotate the key (section 4) once appliances carry the key list.
- Turn off OTA updates on .221 (`systemctl disable --now zenplus-updater.timer`) so
  the Git working tree is changed only by developers. Test OTA behaviour on lab and
  reference appliances.
