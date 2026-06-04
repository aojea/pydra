#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "Verifying generated protos..."
make protos > /dev/null 2>&1

if [[ -n "$(git status --porcelain pydra/core/generated proto)" ]]; then
    echo "ERROR: Generated protos are out of date. Please run 'make protos' and commit the changes."
    git diff pydra/core/generated proto
    exit 1
fi
echo "Protos are up to date!"
