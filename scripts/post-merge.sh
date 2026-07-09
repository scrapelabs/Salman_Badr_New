#!/bin/bash
set -e
pnpm install --frozen-lockfile
cd artifacts/matchminer
python3 manage.py migrate --noinput
