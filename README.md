# text2cli POC

`text2cli` is a local proof of concept for an agent-native transactional workspace database. It exposes a CLI command surface that looks like workspace and filesystem operations, while the storage layer is an MVCC-style commit log over SQLite in WAL mode.

This is intentionally not a POSIX replacement. The goal is to validate the smaller claim: agents can operate against a transactional workspace abstraction with snapshot reads, isolated staging, atomic commits, conflict-aware merges, and an event stream, without needing a full VM or a real host filesystem for every task.

## Why this POC exists

- Reduce per-agent environment cost by moving the default interaction model from "full machine" to "transactional workspace".
- Preserve the agent's CLI execution model with commands such as `fs.read`, `fs.write`, `ws.commit`, and `ws.merge`.
- Make every mutation auditable and replayable through commits and events.
- Provide a path to future backends such as FoundationDB or TiKV without changing the CLI facade.

## What is implemented

- SQLite-backed metadata store with WAL enabled
- Content-addressed blobs
- Named workspaces with isolated staged changes
- Atomic commit flow with policy hook enforcement
- Optimistic conflict detection on merge
- MVCC snapshot reads with time-travel (`fs.read --at <commit>`)
- Named immutable snapshots (`ws.snapshot`)
- Staged rollback (`ws.rollback`) and workspace reset (`ws.reset`)
- Path search (`fs.find`) and content search (`fs.grep`)
- Policy hooks: path deny, max file size, commit message prefix
- Event log for `pre_commit`, `post_commit`, `pre_merge`, `post_merge`, `merge_conflict`, `snapshot_created`, `staged_rollback`, `workspace_reset`
- Concurrent multi-agent access with workspace-level isolation
- JSON-first CLI output for agent consumption

## Project layout

- [`src/text2cli/cli.py`](/Volumes/ExternalSSD/workplace/text2cli/src/text2cli/cli.py): CLI facade
- [`src/text2cli/db.py`](/Volumes/ExternalSSD/workplace/text2cli/src/text2cli/db.py): storage engine and workspace semantics
- [`src/text2cli/web.py`](/Volumes/ExternalSSD/workplace/text2cli/src/text2cli/web.py): web server and JSON API
- [`src/text2cli/agent.py`](/Volumes/ExternalSSD/workplace/text2cli/src/text2cli/agent.py): minimal tool-using agent
- [`docs/ARCHITECTURE.md`](/Volumes/ExternalSSD/workplace/text2cli/docs/ARCHITECTURE.md): design notes and future evolution
- [`docs/IMPLEMENTATION.md`](/Volumes/ExternalSSD/workplace/text2cli/docs/IMPLEMENTATION.md): engineering and code walkthrough
- [`tests/test_poc.py`](/Volumes/ExternalSSD/workplace/text2cli/tests/test_poc.py): regression tests for the core workflow
- [`tests/test_web.py`](/Volumes/ExternalSSD/workplace/text2cli/tests/test_web.py): web and agent flow tests

## Quick start

Create a virtual environment if you want one, then run:

```bash
python3 -m pip install -e .
t2 init
t2 fs.write --workspace main README.md --text "hello workspace"
t2 ws.commit --workspace main -m "seed readme"
t2 ws.create agent-a --from main
t2 fs.patch --workspace agent-a README.md --find "hello" --replace "hello agent"
t2 ws.commit --workspace agent-a -m "personalize readme"
t2 ws.merge --source agent-a --target main -m "merge agent-a"
t2 fs.read --workspace main README.md
```

### MVCC and search features

```bash
# Time-travel: read file at a previous commit
t2 fs.read --workspace main README.md --at <commit_id>

# Named snapshot
t2 ws.snapshot release-v1 --workspace main

# Rollback staged changes
t2 ws.rollback --workspace main

# Reset workspace to a previous commit
t2 ws.reset --workspace main --to <commit_id>

# Search files by path pattern
t2 fs.find --workspace main "*.py"

# Search file contents by regex
t2 fs.grep --workspace main "import os"
```

## Run the AI-native app shell

The web layer adds:

- a single-page chat and workspace UI
- upload, edit, delete, and commit flows for workspace files
- a minimal tool-driven agent that can operate on the workspace through chat

Start it with:

```bash
python3 -m pip install -e .
t2-web --host 127.0.0.1 --port 8770
```

Open [http://127.0.0.1:8770](http://127.0.0.1:8770).

The current agent is intentionally constrained. It supports commands such as:

- `/ls`
- `/read README.md`
- `/write docs/plan.md hello`
- `/append docs/plan.md more`
- `/delete docs/plan.md`
- `/commit seed workspace`
- `/find *.py`
- `/grep TODO`
- `/rollback`
- `/snapshot v1.0`
- `/overview`

It also understands Chinese prompts like `列出文件`, `读取 README.md`, `查找 *.py`, `搜索 TODO`, `回滚`, `快照 v1`.

## Docker

Build the image. The `test` stage runs the full test suite during build:

```bash
docker build -t text2cli:latest .
```

If you only want to verify the containerized test stage explicitly:

```bash
docker build --target test -t text2cli:test .
```

Run the app:

```bash
docker run --rm -p 8770:8770 -v text2cli-data:/data text2cli:latest
```

Open [http://127.0.0.1:8770](http://127.0.0.1:8770).

The container stores its SQLite database at `/data/workspace.db`.

## Demo

Run the scripted demo:

```bash
bash examples/demo.sh
```

The script exercises:

- init
- commit on `main`
- fork into two workspaces
- successful merge from one branch
- merge conflict from another branch
- event stream inspection

## Test

```bash
python3 -m unittest discover -s tests -v
```

Containerized test path:

```bash
docker build --target test .
```

## Future direction

This POC keeps the CLI surface stable and intentionally keeps the backend simple. The obvious next steps are:

- replace SQLite with FoundationDB or TiKV for distributed metadata and multi-writer concurrency
- move large blobs to object storage (S3/MinIO)
- add `exec.run` for controlled process execution in ephemeral micro-sandboxes
- plug an LLM into `agent.py` to replace rule-based dispatch
- add webhook-based policy hooks for external validation systems
- expose the CLI as an MCP server for direct LLM tool calling
