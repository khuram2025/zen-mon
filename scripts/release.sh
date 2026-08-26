#!/usr/bin/env bash
# Build, verify, and publish a ZenPlus OTA release from the protected main tree.
#
# Usage:
#   bash scripts/release.sh <version> "<changelog>" [severity] [rollout] [min-version] [scope]
#
# severity: normal (default) | security | critical | optional
# rollout:  none (default) | canary | percentage | full
# scope:    bundled (default) | appliance-only
#
# A release is published without a rollout by default. Promote it only after a
# genuine prior-version appliance has completed the canary verification.

set -euo pipefail

ZENPLUS_DIR="${ZENPLUS_DIR:-/opt/zenplus}"
PYTHON="${ZENPLUS_RELEASE_PYTHON:-${ZENPLUS_DIR}/venv/bin/python}"
PRIVATE_KEY="${ZENPLUS_RELEASE_PRIVATE_KEY:-${HOME}/keys/zentryc-release.key}"
PUBLIC_KEY="${ZENPLUS_RELEASE_PUBLIC_KEY:-${ZENPLUS_DIR}/updater/keys/zentryc-release.pub}"
CREDS="${HOME}/.zenplus-admin-creds"

if [ "$#" -lt 2 ]; then
    echo "usage: bash scripts/release.sh <version> \"<changelog>\" [severity] [rollout] [min-version] [scope]" >&2
    exit 1
fi

VERSION="$1"
CHANGELOG="$2"
SEVERITY="${3:-normal}"
ROLLOUT="${4:-none}"
MIN_VERSION="${5:-${ZENPLUS_MIN_VERSION:-}}"
RELEASE_SCOPE="${6:-${ZENPLUS_RELEASE_SCOPE:-bundled}}"
ZUP="${ZENPLUS_RELEASE_DIR:-/tmp/zenplus-releases}/update-${VERSION}.zup"

case "$SEVERITY" in
    normal|security|critical|optional) ;;
    *) echo "invalid severity: $SEVERITY" >&2; exit 1 ;;
esac
case "$ROLLOUT" in
    none|canary|percentage|full) ;;
    *) echo "invalid rollout: $ROLLOUT" >&2; exit 1 ;;
esac
case "$RELEASE_SCOPE" in
    bundled|appliance-only) ;;
    *) echo "invalid release scope: $RELEASE_SCOPE" >&2; exit 1 ;;
esac

if [ "$(id -u)" -eq 0 ]; then
    echo "run this script as the repository/release-key owner, not root" >&2
    exit 1
fi

cd "$ZENPLUS_DIR"

test -x "$PYTHON" || { echo "missing release Python: $PYTHON" >&2; exit 1; }
test -r "$PRIVATE_KEY" || { echo "missing release signing key: $PRIVATE_KEY" >&2; exit 1; }
test -r "$PUBLIC_KEY" || { echo "missing release public key: $PUBLIC_KEY" >&2; exit 1; }
test -r "$CREDS" || { echo "missing zentryc admin credentials: $CREDS" >&2; exit 1; }

git fetch origin --prune
test "$(git branch --show-current)" = "main" || {
    echo "release must be cut from main" >&2
    exit 1
}
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" || {
    echo "local main is not synchronized with origin/main" >&2
    exit 1
}
test -z "$(git status --porcelain)" || {
    echo "release worktree is not clean" >&2
    git status --short >&2
    exit 1
}
test "$(head -n 1 .version)" = "$VERSION" || {
    echo ".version does not match requested release $VERSION" >&2
    exit 1
}

export ZENPLUS_DIR
export ZENPLUS_RELEASE_PRIVATE_KEY="$PRIVATE_KEY"
export ZENPLUS_RELEASE_PUBLIC_KEY="$PUBLIC_KEY"

"$PYTHON" scripts/build-release.py lint-migrations
BUILD_ARGS=(
    build
    --version "$VERSION"
    --changelog "$CHANGELOG"
    --severity "$SEVERITY"
)
if [ -n "$MIN_VERSION" ]; then
    BUILD_ARGS+=(--min-version "$MIN_VERSION")
fi
if [ "$RELEASE_SCOPE" = "appliance-only" ]; then
    BUILD_ARGS+=(--skip-agent-artifacts)
fi
"$PYTHON" scripts/build-release.py "${BUILD_ARGS[@]}"

VERIFY_ARGS=(
    scripts/verify-ota-release.py
    "$ZUP"
    "$PUBLIC_KEY"
    --version "$VERSION"
)
if [ "$RELEASE_SCOPE" = "appliance-only" ]; then
    VERIFY_ARGS+=(--appliance-only)
else
    AGENT_VERSION="$(sed -n 's/.*AgentVersion[[:space:]]*=[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' ZenPlus_Agent/internal/model/model.go)"
    test -n "$AGENT_VERSION" || { echo "cannot determine Windows AgentVersion" >&2; exit 1; }
    VERIFY_ARGS+=(--agent-version "$AGENT_VERSION")
fi
"$PYTHON" "${VERIFY_ARGS[@]}"

PUBLISH_ARGS=(
    publish
    --file "$ZUP"
    --version "$VERSION"
    --changelog "$CHANGELOG"
    --severity "$SEVERITY"
)
if [ -n "$MIN_VERSION" ]; then
    PUBLISH_ARGS+=(--min-version "$MIN_VERSION")
fi
if [ "$RELEASE_SCOPE" = "appliance-only" ]; then
    PUBLISH_ARGS+=(--skip-agent-artifacts)
fi
if [ "$ROLLOUT" != "none" ]; then
    PUBLISH_ARGS+=(
        --rollout "$ROLLOUT"
        --rollout-pct "${ZENPLUS_ROLLOUT_PCT:-100}"
    )
    if [ -n "${ZENPLUS_ROLLOUT_GROUP:-}" ]; then
        PUBLISH_ARGS+=(--rollout-group "$ZENPLUS_ROLLOUT_GROUP")
    fi
fi

"$PYTHON" scripts/build-release.py "${PUBLISH_ARGS[@]}"
