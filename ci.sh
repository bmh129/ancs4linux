#!/bin/bash
set -eu

python3 -m venv .venv
source .venv/bin/activate
pip install --quiet pytest
pip install --quiet -e .

pytest
