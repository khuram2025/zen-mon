# ZenPlus 1.23.19

Cloned agent installations now appear individually in Agent Fleet for normal approval. Each installation is bound to its reported agent UID and protected pending-secret hash. Repeated polls update its own pending entry, and approval hands it a unique identity and API key. Clones no longer overwrite a single conflict candidate or receive the 409 response that caused long registration backoff.

Server linking preserves separate monitoring histories even when clones share a hostname. Agent Fleet displays the original host and offers explicit reinstall-of-original and block actions for pending clones. Migration 128 converts remaining legacy conflict candidates to independent pending entries and is replay-safe.

## Upgrade and recovery

- Minimum supported installed version: **1.23.9**, retaining the existing required upgrade path.
- Source change: Claude commit `c65ff2401d6601a0e71520585e98278f6d919a3b`, based on released main `ff3baee948338bb5b9465c9d5e30216e8ff2db8b`.
- Complete historical migration inventory and existing security, permissions, preflight, schema and rollback safeguards remain included.
- Bundled Windows Agent **1.14.3** packages are reused from the verified signed artifact store; no agent rebuild is required.
- Updater code/database backups provide recovery. Migration 128 clears legacy conflict fields only after retaining candidate identity and protected secret hashes in separate pending entries and resolution records. Rollback cannot erase this new registration history safely; retain database backups and investigate before restoring them.
- Installations cloned with the same unchanged pending secret remain indistinguishable. Templates must have a separate agent registration secret per installed machine.

## Validation

Claude reported 1,007 Python tests, the strict dashboard build and route smoke checks passing, plus live simulated enrollment, repeated polling, approval, distinct server linking, credential handover, reinstall and blocking checks on the development appliance. Migration conversion was exercised in a rolled-back transaction there. These checks used simulated API clients, not actual cloned VMs.

Release publication requires the five repository CI jobs, migration lint, independent signed-package verification and catalog/rollout checks. Publication IDs, package hash and final verification evidence are recorded separately after release.
