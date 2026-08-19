# ZenPlus GitHub, Multi-Machine Development, Release, and Publishing Runbook

> **Purpose:** one operational source of truth for developing ZenPlus from two appliances, synchronizing through GitHub, merging safely, publishing signed ZenPlus OTA releases to zentryc.com, and publishing ZenPlus knowledge-base pages on zentryc.com.
>
> **Audience:** ZenPlus developers, reviewers, release engineers, and website/KB publishers.
>
> **Last verified:** 2026-08-19.
>
> **Security:** commands in this document intentionally contain no passwords, API tokens, private keys, or production database credentials. Keep those in the approved secret manager. Never add them to Git, documentation, chat, shell history, screenshots, or release packages.

---

## 1. Systems and responsibilities

| Name | Current role | Connection/path | Rules |
|---|---|---|---|
| `zenplus` appliance | Primary ZenPlus development/integration appliance | SSH alias `zenplus` currently resolves to `192.168.8.221`, user `zen`; repository `/opt/zenplus` | May build and test. It must not be the central Git source of truth. Only cut signed OTA releases here if the approved signing key and release credentials are present. |
| `zen` appliance | Second ZenPlus development appliance | Configure a stable SSH alias named `zen`; repository should be `/opt/zenplus` | Develop on a separate feature branch. Confirm its IP and SSH key in each developer's local SSH config; do not put a password in this file. |
| GitHub | Central source of truth and collaboration system | Expected source repository: `https://github.com/khuram2025/zen-mon` | All durable source work goes through branches, pull requests, review, and CI. Protect `main`. |
| zentryc.com OTA service | Stores signed `.zup` releases and controls fleet rollout | `https://zentryc.com/api/v1` | Publish only a reviewed commit from protected `main`, preferably first to canary. |
| zentryc.com web/KB host | Runs the public Django website and ZenPlus KB | SSH user `net` on the approved web-host address; Django root `/home/net` | The live directory is currently not Git-managed. Back up before manual publication; migrate it to a private GitHub repository and CI deployment. |

The word **publish** has three different meanings. Do not mix them:

1. **Push source:** send commits and branches to GitHub.
2. **Publish an OTA release:** build, sign, and upload a `.zup` to the zentryc.com release API.
3. **Publish documentation:** deploy Django templates/static files to the zentryc.com web host.

Pushing Git does not automatically perform either website publication or an OTA release unless an approved CI/CD workflow is deliberately configured to do so.

---

## 2. Target architecture

```text
Developer on zenplus ──feature branch──┐
                                      ├── GitHub PR ── CI ── protected main
Developer on zen ──────feature branch──┘                       │
                                                              ├── signed OTA build
                                                              │     └── zentryc.com rollout
                                                              └── approved KB deployment
                                                                    └── zentryc.com website
```

Golden rules:

- GitHub `origin/main` is the only source of truth.
- Do not develop directly on `main`.
- Do not use a deployed appliance repository as the team remote.
- Do not merge every branch simply because it exists. Merge only reviewed, intentional work.
- Do not deploy an uncommitted or dirty working tree.
- Do not force-push `main`.
- Build releases from an exact commit and record that commit in the release evidence.

---

## 3. One-time GitHub setup

### 3.1 Confirm the central repository before pushing

The currently reviewed local repository has only an appliance remote named `live`; it does **not** currently have a GitHub `origin`. Before adding one, confirm that the GitHub repository is the correct organization/repository and inspect its existing branches:

```bash
git ls-remote --heads https://github.com/khuram2025/zen-mon.git
```

Never overwrite an existing GitHub history. If the GitHub `main` and the appliance `main` have unrelated or divergent histories, stop and reconcile them through a reviewed integration branch.

After the repository owner confirms the URL, configure it on both appliances:

```bash
cd /opt/zenplus
git remote add origin git@github.com:khuram2025/zen-mon.git
git fetch origin --prune
git remote -v
```

If `origin` already exists:

```bash
git remote get-url origin
git remote set-url origin git@github.com:khuram2025/zen-mon.git
git fetch origin --prune
```

Keep any appliance-to-appliance remote clearly named so it cannot be mistaken for GitHub:

```bash
git remote rename live appliance-zenplus
```

The appliance remote is optional and should normally be read-only for diagnosis. Collaboration and release promotion must use `origin`.

### 3.2 GitHub authentication

Use a different SSH key for each developer or automation identity. Add only the public key to GitHub. Do not share private keys between people or appliances.

```bash
ssh-keygen -t ed25519 -C "developer-or-appliance-name"
ssh -T git@github.com
```

For CI, prefer GitHub OIDC or a narrowly scoped deployment key. Do not store a personal access token in the repository or appliance `.env` file.

### 3.3 Required repository controls

Protect `main` in GitHub with these settings:

- Require a pull request before merging.
- Require at least one approval from someone other than the author.
- Dismiss stale approvals after new commits.
- Require all CI checks to pass.
- Require resolution of review conversations.
- Block force pushes and branch deletion.
- Restrict direct pushes to release automation only, or block them completely.
- Enable secret scanning, push protection, and dependency alerts.
- Use `CODEOWNERS` for security-sensitive paths such as `updater/`, `scripts/build-release.py`, migrations, authentication, and release keys.

Recommended merge method: squash merge for a small single-purpose feature, or a regular merge commit when the branch contains meaningful reviewed commits. Do not use an automatic “merge all branches” process.

### 3.4 Line-ending safety

SQL migrations are checksum-locked. Windows CRLF conversion can make valid migrations appear modified. The repository should include a reviewed `.gitattributes` policy such as:

```gitattributes
*.sql text eol=lf
*.sh  text eol=lf
*.py  text eol=lf
*.go  text eol=lf
*.yml text eol=lf
*.yaml text eol=lf
```

Until that is committed, use an LF checkout for release validation:

```bash
git config core.autocrlf false
```

Do not update `scripts/migrations.lock` merely to hide a CRLF mismatch.

---

## 4. Normal development from `zenplus` or `zen`

### 4.1 Start new work

Run the same process on either appliance:

```bash
cd /opt/zenplus
git status --short --branch
git fetch origin --prune
git switch main
git pull --ff-only origin main
git switch -c feature/<short-description>
```

Examples:

```text
feature/apm-iis-instrumentation
fix/apm-clock-skew-response
docs/apm-iis-guide
chore/dashboard-type-cleanup
```

Keep one purpose per branch. If two developers are working at the same time, use separate branches even when they are using different machines.

### 4.2 Save and publish work

Review exactly what will be committed:

```bash
git status --short
git diff
git diff --check
git add <specific-files>
git diff --cached
git commit -m "feat(apm): concise description"
git push -u origin HEAD
```

Never use `git add .` without reviewing the result. Never commit runtime secrets, generated release packages, database files, logs, virtual environments, `node_modules`, private keys, or appliance-local updater state.

### 4.3 Continue the same branch on the other appliance

Before leaving the first appliance, commit and push. On the second appliance:

```bash
cd /opt/zenplus
git status --short --branch
git fetch origin --prune
git switch feature/<short-description>
git pull --ff-only origin feature/<short-description>
```

If the branch does not yet exist locally:

```bash
git switch --track origin/feature/<short-description>
```

Do not use an uncommitted working tree or `git stash` as the synchronization mechanism between machines. A temporary WIP commit on a feature branch is safer; clean it up before review if necessary.

Avoid editing the same branch concurrently on both appliances. If unavoidable, coordinate ownership by file or subtask and rebase frequently.

### 4.4 Bring current `main` into a feature branch

```bash
git fetch origin --prune
git switch feature/<short-description>
git rebase origin/main
```

After resolving conflicts:

```bash
git add <resolved-files>
git rebase --continue
```

If the feature branch was already published, use `--force-with-lease` only on that personal feature branch after coordinating with collaborators:

```bash
git push --force-with-lease origin feature/<short-description>
```

Never use `--force` and never rewrite `main`.

---

## 5. Pull request and merge gate

Every pull request should state:

- Why the change is needed.
- The exact scope and affected modules.
- Schema or migration impact.
- Configuration changes and upgrade impact.
- Tests run and their results.
- Screenshots for visible dashboard or documentation changes.
- Rollback or disable procedure.
- Whether an agent, appliance, OTA release, or KB publication is required.

Minimum validation from an LF checkout:

```bash
cd /opt/zenplus
git diff --check origin/main...HEAD
server/venv/bin/python -m pytest server/tests -q
python3 scripts/build-release.py lint-migrations
cd dashboard
npm ci
npm run smoke
```

The release builder intentionally uses `npx vite build`. The stricter `npm run build` also runs `tsc -b` and currently reports known repository-wide TypeScript debt. Track that debt separately; do not misrepresent it as passing. New code should not add new TypeScript errors.

For a new migration:

```bash
python3 scripts/build-release.py lint-migrations --update-lock
python3 scripts/build-release.py lint-migrations
```

Commit the new migration and updated lockfile together. Released migration files are append-only; fix a released migration by adding a new migration.

### Merge procedure

1. CI is green and review is approved.
2. Confirm the PR branch is based on current `origin/main`.
3. Merge through GitHub.
4. Delete the merged feature branch in GitHub.
5. On both appliances:

```bash
git fetch origin --prune
git switch main
git pull --ff-only origin main
```

6. Confirm both appliances report the same commit:

```bash
git rev-parse HEAD
git rev-parse origin/main
```

An old `backup/*` or WIP branch is not a release input unless someone reviews its unique commits and opens an explicit PR.

---

## 6. Release version and tag policy

Use semantic versions:

- Patch: compatible fixes, for example `1.15.5` → `1.15.6`.
- Minor: compatible features, for example `1.15.6` → `1.16.0`.
- Major: breaking change, for example `1.x` → `2.0.0`.

Before tagging, update `.version` exactly as required by the release tooling, commit it through a release PR, and verify that the version is higher than every release already published on zentryc.com.

Create an annotated tag only on protected `main` after the merge and validation:

```bash
git switch main
git pull --ff-only origin main
git tag -a v1.15.6 -m "ZenPlus 1.15.6"
git push origin v1.15.6
```

Tags must be immutable. If a release is bad, publish a higher corrected version; do not move or replace the old tag.

Record this release evidence:

```text
Version:
Git commit:
Git tag:
Pull request:
CI run:
Builder host:
Package SHA-256:
Manifest signature verified:
Rollout stage:
Canary appliance(s):
Health verification:
Rollback decision/owner:
```

---

## 7. Publishing a signed ZenPlus OTA release to zentryc.com

This is application/appliance publication, not KB publication.

Canonical detailed references:

- [Release Runbook](15-RELEASE-RUNBOOK.md)
- [OTA Release Workflow](../docs/OTA-RELEASE-WORKFLOW.md)
- [Migration Runner](18-MIGRATION-RUNNER.md)

### 7.1 Release preflight

On the approved Linux release builder:

```bash
cd /opt/zenplus
git status --short --branch
git fetch origin --prune
git switch main
git pull --ff-only origin main
git rev-parse HEAD
git describe --tags --always
```

The working tree must be clean and `HEAD` must equal `origin/main`. Then run:

```bash
server/venv/bin/python -m pytest server/tests -q
python3 scripts/build-release.py lint-migrations
cd dashboard && npm ci && npm run smoke && cd ..
```

Confirm before continuing:

- The requested version is correct and monotonically higher.
- The changelog describes only changes in the selected commit range.
- The Windows agent MSI and other required immutable agent artifacts are present and have correct versions/checksums.
- The Ed25519 private release key exists only on the approved builder and has restrictive permissions.
- The matching public key is installed and its fingerprint matches the fleet trust anchor.
- OTA administrator credentials are available through the approved `0600` credentials file or secret injection mechanism.
- No private key, API key, `.env`, subscription state, or credential file is tracked by Git or staged into the package.

### 7.2 Build, verify, and publish

The repository wrapper handles ownership, the approved build user, signature verification, temporary credential staging, and cleanup:

```bash
sudo bash scripts/release.sh \
  1.15.6 \
  "Concise customer-facing changelog" \
  normal \
  canary
```

Prefer `canary` for feature releases. Use `security` or `critical` severity only when appropriate and approved.

For explicit build/inspection before upload:

```bash
sudo -u zenplus env HOME=/opt/zenplus python3 scripts/build-release.py build \
  --version 1.15.6 \
  --changelog "Concise customer-facing changelog" \
  --severity normal

sudo -u zenplus env HOME=/opt/zenplus \
  server/venv/bin/python scripts/build-release.py publish \
  --file /tmp/zenplus-releases/update-1.15.6.zup \
  --version 1.15.6 \
  --changelog "Concise customer-facing changelog" \
  --severity normal \
  --rollout canary
```

Never publish an unsigned build. Never copy the private key into a release package.

### 7.3 Canary verification and promotion

After publication:

```bash
sudo -u zenplus env HOME=/opt/zenplus \
  server/venv/bin/python scripts/build-release.py list
```

On the canary appliance, verify:

```bash
sudo zenplus status
sudo /opt/zenplus/scripts/sync-schema.py --check
curl -fsS http://localhost:8000/api/v1/system/health
sudo journalctl -u zenplus-updater -n 200 --no-pager
```

Also run product-specific smoke tests in the UI and confirm the dashboard version, service health, agent download, database migrations, and the feature changed by the release.

Promote only after the observation window is clean:

```bash
server/venv/bin/python scripts/build-release.py rollout \
  --version 1.15.6 \
  --stage percentage \
  --pct 25

server/venv/bin/python scripts/build-release.py rollout \
  --version 1.15.6 \
  --stage full \
  --pct 100
```

Use the exact supported arguments shown by `scripts/build-release.py rollout --help`; the command interface is authoritative if it differs from an older example.

### 7.4 Failure and rollback

- Pause or abort the rollout in zentryc.com immediately.
- Preserve updater logs and the failing package metadata.
- The appliance updater automatically runs the manifest rollback steps when application fails.
- Confirm the appliance returned to the previous version and services are healthy.
- Fix forward on a new branch and publish a higher version.
- Do not replace an already-published artifact or reuse its version number.

---

## 8. Publishing ZenPlus KB documentation to zentryc.com

This section concerns pages such as `https://zentryc.com/kb/zenplus/apm/`. It does not publish an appliance update.

### 8.1 Current verified website layout

```text
/home/net/
├── manage.py
├── zentryc_project/                  # Django settings and root URLs
├── core/
│   ├── urls.py                       # KB routes
│   ├── views.py                      # KB views
│   ├── tests.py                      # route/page tests
│   └── templates/core/               # KB HTML templates
│       ├── kb_zenplus_apm.html
│       ├── kb_zenplus_apm_*.html
│       └── kb_zenplus_udt_*.html
├── static/img/zenplus/               # source screenshots and KB images
├── staticfiles/                      # collectstatic output; do not hand-edit
└── zentryc_env/                      # Python virtual environment
```

Running services:

```text
zentryc.service          Django/Gunicorn website
zentryc-celery.service   Celery worker/beat
nginx.service            TLS/reverse proxy/static delivery
```

Current risk: `/home/net` is a live mutable deployment and is not presently a Git working tree. Direct editing provides no reliable review history. The professional target is a separate private GitHub repository for the website with protected `main`, tests, artifact deployment, and rollback by commit.

### 8.2 What a KB change may include

- Article/index HTML in `core/templates/core/`.
- New route in `core/urls.py`.
- View metadata in `core/views.py`.
- Route/page coverage in `core/tests.py`.
- Sitemap registration in `core/sitemaps.py`.
- Images in `static/img/zenplus/<module>/`.
- Links in the ZenPlus KB index and `templates/llms.txt` when a new page is introduced.

Every screenshot must be readable at full size, contain no passwords/API keys/customer data, have meaningful alt text, and use a stable descriptive filename. Prefer PNG for UI text and WebP/JPEG for photographic content.

### 8.3 Recommended GitHub website workflow

1. Create or identify the private central website repository; do not guess its name or overwrite a repository.
2. Import the current `/home/net` application source while excluding secrets and runtime data.
3. Protect its `main` branch and require tests/review.
4. Store deployment secrets in a protected GitHub Environment named `production`.
5. Require manual approval for the production deployment job.
6. Deploy an immutable commit/artifact, not an editor's working directory.

The website repository must exclude at least:

```gitignore
.env
*.sqlite3
*.log
zentryc_env/
staticfiles/
backups/
packages/
agent-releases/
keys/
*.key
*.pem
__pycache__/
```

A deployment job should perform, in order:

1. Build/test from the approved commit.
2. Back up the current templates/static assets and record the deployed commit.
3. Upload to a staging directory on the web host.
4. Synchronize approved application files without touching `.env`, runtime packages, databases, logs, keys, or backups.
5. Run Django checks and tests.
6. Run migrations only when the reviewed change includes them.
7. Run `collectstatic`.
8. Restart `zentryc`; restart `zentryc-celery` only if its code/config changed.
9. Reload nginx only if nginx configuration changed.
10. Run external HTTP smoke checks and retain the deployment log.

### 8.4 Safe manual procedure until website CI exists

Manual publication is an exception. Use the approved zentryc web-host SSH address and the `net` account; do not publish via the Cloudflare-facing `zentryc.com` hostname on port 22.

First create a dated backup on the server:

```bash
cd /home/net
KB_BACKUP_DIR="/home/net/backups/kb-zenplus-$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$KB_BACKUP_DIR/core/templates/core" "$KB_BACKUP_DIR/static/img/zenplus"

cp -a core/templates/core/kb_zenplus_apm*.html \
  "$KB_BACKUP_DIR/core/templates/core/"
cp -a static/img/zenplus/apm-guide \
  "$KB_BACKUP_DIR/static/img/zenplus/" 2>/dev/null || true
cp -a core/urls.py core/views.py core/tests.py core/sitemaps.py "$KB_BACKUP_DIR/"
printf '%s\n' "$KB_BACKUP_DIR"
```

Upload proposed files to a new staging directory, not directly over live files. Review names and diffs, then copy only the approved files into their matching paths under `/home/net`.

Validate before restarting:

```bash
cd /home/net
./zentryc_env/bin/python manage.py check
./zentryc_env/bin/python manage.py test core
./zentryc_env/bin/python manage.py collectstatic --noinput
```

If any command fails, stop and restore the backup. When all checks pass:

```bash
sudo systemctl restart zentryc
sudo systemctl status zentryc --no-pager
```

Only if Celery code/configuration changed:

```bash
sudo systemctl restart zentryc-celery
sudo systemctl status zentryc-celery --no-pager
```

Only if nginx configuration changed:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Verify locally on the web host and externally:

```bash
curl -fsSI http://127.0.0.1/kb/zenplus/apm/
curl -fsSI https://zentryc.com/kb/zenplus/apm/
curl -fsS https://zentryc.com/kb/zenplus/apm/ > /dev/null
```

Open the pages in a browser and verify desktop/mobile layout, code blocks, screenshots at full size, previous/next links, breadcrumbs, canonical metadata, and all module links. Test each new static image URL directly.

### 8.5 KB rollback

If an article returns errors or renders incorrectly:

1. Restore the affected files from the recorded `KB_BACKUP_DIR`.
2. Run `manage.py check` and the relevant tests.
3. Run `collectstatic --noinput` if static files changed.
4. Restart `zentryc`.
5. Re-run local and external HTTP smoke checks.
6. Record what was reverted and why.

Do not delete the last known-good backup until the new publication has been verified and captured in Git.

---

## 9. CI checks recommended for GitHub

Create separate required jobs so failures are easy to diagnose:

| Job | Required checks |
|---|---|
| `backend` | Install `server/requirements.txt`; run `pytest server/tests -q` from an LF Linux checkout. |
| `migrations` | Run `scripts/build-release.py lint-migrations`; fail on drift or an unlocked new migration. |
| `dashboard-smoke` | `npm ci`, `npm run smoke`; retain bundle-size warnings. |
| `dashboard-types` | Run `npm run build`; initially allowed to report the known baseline, then make required after the type-debt cleanup. |
| `go` | Run Go tests, formatting/vet, and production binary compilation. |
| `secrets` | Secret scanning plus checks that no private key/updater state/release credential is packaged. |
| `release-dry-run` | On tags, build the `.zup`, verify checksums/signature/layout, and store it as a protected artifact without publishing. |
| `website-kb` | Django `check`, `test core`, template/static link checks, and HTML accessibility checks. |

Production OTA publication and production website deployment must use protected environments with an explicit approval gate.

---

## 10. Daily and release checklists

### Developer starts work

- [ ] Working tree reviewed; unrelated local changes preserved.
- [ ] `origin` points to the approved GitHub repository.
- [ ] `git fetch origin --prune` completed.
- [ ] Feature branch created from current `origin/main`.
- [ ] No secrets or runtime files in the change.

### Developer changes machines

- [ ] Work committed and pushed from the first appliance.
- [ ] Correct feature branch fetched on the second appliance.
- [ ] `git pull --ff-only` succeeded.
- [ ] Both machines are not editing the same files concurrently without coordination.

### Pull request merge

- [ ] Scope and rollback explained.
- [ ] Required tests pass from LF/Linux CI.
- [ ] Migration and configuration impact reviewed.
- [ ] UI/KB screenshots reviewed where applicable.
- [ ] Approval from another person.
- [ ] Merged through GitHub; no direct push or force push.

### OTA release

- [ ] Clean protected `main`; exact commit/tag recorded.
- [ ] Version and changelog approved.
- [ ] Backend, migration, dashboard, Go, and artifact checks pass.
- [ ] Package signature and SHA-256 verified.
- [ ] Canary rollout selected and verified.
- [ ] Promotion evidence recorded.
- [ ] Rollback owner available during the release window.

### KB publication

- [ ] Change exists in the website GitHub PR, or manual-exception approval recorded.
- [ ] Live files backed up before modification.
- [ ] No credentials/customer data in text or screenshots.
- [ ] Django checks/tests and `collectstatic` pass.
- [ ] Correct services restarted/reloaded.
- [ ] External URLs, mobile layout, screenshots, and links verified.
- [ ] Deployed commit and backup path recorded.

---

## 11. Current-state notes and next actions

As verified on 2026-08-19:

- The integrated ZenPlus `main` is commit `67aecc40d28ee6940dc563f9bc40e7ef617fd1ff` on the appliance remote.
- The reviewed local clone currently has `live -> zenplus:/opt/zenplus`; no GitHub `origin` is configured there.
- The current release-line validation passed 333 backend tests with 42 skipped from an LF checkout, and the dashboard route/Vite smoke build passed.
- The zentryc.com Django website source under `/home/net` is currently not a Git working tree.
- The `zen` appliance address/SSH alias still needs to be confirmed and standardized on every developer workstation.

Priority actions:

1. Confirm the authoritative GitHub repository and reconcile its `main` with commit `67aecc4` without force-pushing.
2. Add `origin` on both appliances and protect GitHub `main`.
3. Add Linux CI and an explicit LF `.gitattributes` policy.
4. Confirm and document the stable SSH alias for `zen`.
5. Put the zentryc.com website source in a separate private GitHub repository.
6. Replace direct website edits with an approved staging/CI deployment.
7. Keep the appliance release-signing private key only on the approved builder or move signing to a managed service/HSM.

When any hostname, repository URL, service name, deployment path, or release command changes, update this file in the same pull request that changes the system.
