#!/usr/bin/env bash
set -euo pipefail

DB_PATH="${1:-/tmp/text2cli-demo.db}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

rm -f "$DB_PATH"

python3 -m text2cli init --db "$DB_PATH"
python3 -m text2cli fs.write --db "$DB_PATH" --workspace main README.md --text "hello workspace"
python3 -m text2cli ws.commit --db "$DB_PATH" --workspace main -m "seed workspace"

python3 -m text2cli ws.create agent-a --db "$DB_PATH" --from main
python3 -m text2cli ws.create agent-b --db "$DB_PATH" --from main

python3 -m text2cli fs.patch --db "$DB_PATH" --workspace agent-a README.md --find "hello" --replace "hello agent-a"
python3 -m text2cli ws.commit --db "$DB_PATH" --workspace agent-a -m "agent-a change"
python3 -m text2cli ws.merge --db "$DB_PATH" --source agent-a --target main -m "merge agent-a"

python3 -m text2cli fs.patch --db "$DB_PATH" --workspace agent-b README.md --find "hello" --replace "hello agent-b"
python3 -m text2cli ws.commit --db "$DB_PATH" --workspace agent-b -m "agent-b change"

set +e
python3 -m text2cli ws.merge --db "$DB_PATH" --source agent-b --target main -m "merge agent-b"
STATUS=$?
set -e

echo "merge agent-b exit code: $STATUS"
python3 -m text2cli fs.read --db "$DB_PATH" --workspace main README.md
python3 -m text2cli events --db "$DB_PATH" --limit 20
