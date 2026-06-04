#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "Verifying code with ruff..."

# Install ruff in the virtual environment if it doesn't exist
if [ ! -f ".venv/bin/ruff" ]; then
    echo "Installing ruff..."
    .venv/bin/pip install ruff --index-url https://pypi.org/simple > /dev/null 2>&1
fi

if ! .venv/bin/ruff check .; then
    echo "ERROR: Linting failed! Please fix the issues above."
    exit 1
fi
echo "Linting passed!"
