#!/usr/bin/env bash
set -o errexit
set -o nounset
set -o pipefail

PYTHONPATH=. .venv/bin/pytest -v -s tests/test_integration.py
