#!/usr/bin/env python3
"""Concurrent LLM Agent benchmark.

Spawns N agents, each in its own workspace, sending real tasks to GLM.
Measures latency, tool calls, and success rates.

Usage:
    GLM_API_KEY=<key> python3 test_script/20260320-concurrent-llm-bench.py [--agents 4] [--model glm-4-flash]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from text2cli.agent import LLMWorkspaceAgent
from text2cli.db import WorkspaceDB
from text2cli.llm import GLMClient, LLMClientError

TASKS = [
    "创建一个名为 docs/plan.md 的文件, 内容是一个项目计划模板, 包含目标、时间线、风险三个章节, 然后提交",
    "列出当前工作区所有文件, 然后创建一个 summary.txt 总结文件列表",
    "搜索工作区中包含 '项目' 的文件, 把搜索结果写入 search-results.txt",
    "创建 src/hello.py 文件, 内容是一个打印 hello world 的程序, 然后用 wc 统计行数",
    "创建 notes/todo.md, 内容包含 5 条待办事项, 然后生成目录(toc)",
    "创建 config.yaml 文件, 内容是数据库配置模板, 再创建 config.dev.yaml 作为开发环境配置, 然后提交",
    "写一个 README.md 文件介绍本工作区的用途, 不少于 3 段, 然后提交",
    "创建 data/users.csv 文件包含 5 行用户数据, 然后用 sort 排序查看结果",
]


def run_agent(agent_id: int, db: WorkspaceDB, llm_client: GLMClient) -> dict:
    ws_name = f"bench-agent-{agent_id}"
    task = TASKS[agent_id % len(TASKS)]

    try:
        db.create_workspace(ws_name, from_workspace="main")
    except Exception:
        pass

    agent = LLMWorkspaceAgent(db, llm_client)

    t0 = time.monotonic()
    try:
        result = agent.handle_message(ws_name, task)
        elapsed = time.monotonic() - t0
        return {
            "agent_id": agent_id,
            "workspace": ws_name,
            "status": result.get("status", "unknown"),
            "reply_len": len(result.get("reply", "")),
            "tool_calls": len(result.get("actions", [])),
            "llm_rounds": result.get("llm_rounds", 0),
            "elapsed_s": round(elapsed, 2),
            "error": None,
        }
    except Exception as exc:
        elapsed = time.monotonic() - t0
        return {
            "agent_id": agent_id,
            "workspace": ws_name,
            "status": "error",
            "reply_len": 0,
            "tool_calls": 0,
            "llm_rounds": 0,
            "elapsed_s": round(elapsed, 2),
            "error": str(exc),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Concurrent LLM Agent benchmark")
    parser.add_argument("--agents", type=int, default=4, help="Number of concurrent agents")
    parser.add_argument("--model", default="glm-4-flash", help="GLM model name")
    parser.add_argument("--api-key", default=None, help="GLM API key")
    args = parser.parse_args()

    try:
        llm_client = GLMClient(api_key=args.api_key, model=args.model, timeout=120)
    except LLMClientError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "bench.db"
        db = WorkspaceDB(db_path)
        db.init()
        db.write_file("main", "README.md", "# Benchmark Workspace\n")
        db.commit_workspace("main", "seed")

        print(f"=== Concurrent LLM Agent Benchmark ===")
        print(f"Agents: {args.agents} | Model: {args.model}")
        print(f"DB: {db_path}")
        print()

        results: list[dict] = []
        t_start = time.monotonic()

        with ThreadPoolExecutor(max_workers=args.agents) as pool:
            futures = {
                pool.submit(run_agent, i, db, llm_client): i
                for i in range(args.agents)
            }
            for future in as_completed(futures):
                r = future.result()
                results.append(r)
                status_icon = "✓" if r["status"] == "ok" else "✗"
                print(f"  {status_icon} Agent-{r['agent_id']:02d}: {r['elapsed_s']:>6.1f}s | {r['tool_calls']} tools | {r['llm_rounds']} rounds | {r['reply_len']} chars")
                if r["error"]:
                    print(f"    ERROR: {r['error'][:200]}")

        t_total = time.monotonic() - t_start

        ok = [r for r in results if r["status"] == "ok"]
        fail = [r for r in results if r["status"] != "ok"]
        avg_elapsed = sum(r["elapsed_s"] for r in ok) / len(ok) if ok else 0
        avg_tools = sum(r["tool_calls"] for r in ok) / len(ok) if ok else 0
        avg_rounds = sum(r["llm_rounds"] for r in ok) / len(ok) if ok else 0

        print()
        print(f"=== Summary ===")
        print(f"Total wall time: {t_total:.1f}s")
        print(f"Success: {len(ok)}/{len(results)}")
        print(f"Avg latency: {avg_elapsed:.1f}s")
        print(f"Avg tool calls: {avg_tools:.1f}")
        print(f"Avg LLM rounds: {avg_rounds:.1f}")

        if fail:
            print(f"\nFailed agents:")
            for r in fail:
                print(f"  Agent-{r['agent_id']}: {r['error'][:200]}")

        files = db.list_files("main")
        print(f"\nMain workspace files: {len(files['files'])}")

        workspaces = db.list_workspaces()
        print(f"Total workspaces: {len(workspaces['workspaces'])}")

    return 0 if not fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
