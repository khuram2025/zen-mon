#!/bin/sh
# Re-apply the unprivileged Support runtime after an OTA rollback.
#
# Rollback intentionally restores application code but must never restore the
# legacy root worker, direct systemctl sudo wildcard, or broad polkit rule. The
# setup script is loaded from the signed release payload while its runtime paths
# remain rooted at /opt/zenplus, so it also works when the prior application
# version did not yet contain the secure units.

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
release_code_dir=$(dirname -- "${script_dir}")

ZENPLUS_DIR=/opt/zenplus \
ZENPLUS_SOURCE_DIR="${release_code_dir}" \
    "${script_dir}/setup-support.sh"
