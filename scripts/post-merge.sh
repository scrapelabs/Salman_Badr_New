#!/bin/bash
set -e
pnpm install --frozen-lockfile
cd artifacts/permitlify
python3 manage.py migrate --noinput
