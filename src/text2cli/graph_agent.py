"""LangGraph ReAct agent with Langfuse observability.

This module is the primary agent implementation. It uses LangGraph's
``create_react_agent`` with Linux-like workspace tools and traces every
LLM call / tool invocation through Langfuse (when configured).

LLM provider is selected via ``LLM_PROVIDER`` env var:
  - ``openai``  (default) -- ChatOpenAI with custom ``base_url`` (GLM / DeepSeek / Moonshot / local)
  - ``zhipuai`` -- ChatZhipuAI from langchain-community
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from .db import ValidationError, WorkspaceDB
from .workspace_tools import create_workspace_tools

logger = logging.getLogger(__name__)

REACT_SYSTEM_PROMPT = (
    "你是一个运行在事务工作区中的 AI 文件处理助手. "
    "用户用自然语言描述任务, 你通过调用 Linux 风格的命令自主完成任务.\n\n"
    "## 工作环境\n"
    "你操作的是一个事务工作区(类似 Git 仓库):\n"
    "- 所有写操作先进入暂存区(staged), 需要 commit 才永久化\n"
    "- 可以通过 rollback 撤销所有未提交的变更\n\n"
    "## 工作流程\n"
    "1. 先用 ls 或 find 了解工作区中有哪些文件\n"
    "2. 用 cat 读取需要处理的文件内容\n"
    "3. 规划并执行操作(写入/复制/移动/删除/搜索等)\n"
    "4. 完成写操作后, 主动调用 commit 提交变更, 使用简短中文提交消息\n\n"
    "## 代码执行\n"
    "你还可以通过 python_exec 和 shell_exec 编写并运行代码:\n"
    "- 代码在工作区目录中执行, 可以直接用 open() 读写文件\n"
    "- 适合数据处理、批量操作、复杂文本变换等场景\n"
    "- 通过 python_exec 运行 Python 代码, 通过 shell_exec 运行 Shell 命令\n"
    "- print() 的输出会作为结果返回\n\n"
    "## 联网搜索\n"
    "你可以通过 web_search 搜索互联网 (Brave Search API):\n"
    "- 适合查找信息、文档、教程、新闻、时事等任何网络内容\n"
    "- 搜索结果包含标题、链接和摘要, 可以将结果整理后写入文件\n\n"
    "## 注意事项\n"
    "- 用中文回复用户\n"
    "- 操作前先读取文件确认内容, 不要猜测\n"
    "- 完成写操作后主动提交"
)


def create_chat_model(
    *,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
) -> Any:
    """Create a LangChain ChatModel based on configuration.

    Falls back to env vars: LLM_PROVIDER, LLM_API_KEY, LLM_BASE_URL,
    LLM_MODEL, LLM_TEMPERATURE (with GLM_* aliases for backward compat).
    """
    _provider = provider or os.environ.get("LLM_PROVIDER", "openai")
    _api_key = (
        api_key
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("GLM_API_KEY")
        or os.environ.get("ZHIPUAI_API_KEY")
        or ""
    )
    _model = (
        model
        or os.environ.get("LLM_MODEL")
        or os.environ.get("GLM_MODEL")
        or "glm-4-flash"
    )
    _base_url = (
        base_url
        or os.environ.get("LLM_BASE_URL")
        or os.environ.get("GLM_BASE_URL")
        or "https://open.bigmodel.cn/api/paas/v4"
    )
    _temperature = temperature
    if _temperature is None:
        _temperature = float(
            os.environ.get("LLM_TEMPERATURE")
            or os.environ.get("GLM_TEMPERATURE")
            or "0.7"
        )

    if _provider == "zhipuai":
        from langchain_community.chat_models import ChatZhipuAI
        return ChatZhipuAI(
            api_key=_api_key,
            model=_model,
            temperature=_temperature,
        )

    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        api_key=_api_key,
        base_url=_base_url,
        model=_model,
        temperature=_temperature,
    )


def create_langfuse_handler() -> Any | None:
    """Create a Langfuse CallbackHandler if credentials are configured."""
    pub_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sec_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not pub_key or pub_key.startswith("pk-lf-...") or not sec_key or sec_key.startswith("sk-lf-..."):
        return None
    try:
        from langfuse.langchain import CallbackHandler
        handler = CallbackHandler()
        logger.info("Langfuse tracing enabled (host=%s)", os.environ.get("LANGFUSE_HOST", "default"))
        return handler
    except ImportError:
        logger.warning("langfuse not installed, tracing disabled")
        return None
    except Exception:
        logger.exception("Failed to initialize Langfuse handler")
        return None


class LangGraphAgent:
    """ReAct agent built on LangGraph's ``create_react_agent``.

    For each user message the agent:
    1. Creates workspace-bound tools (including code execution if FUSE is available)
    2. Builds a ReAct graph with the configured LLM
    3. Invokes the graph with Langfuse tracing (if configured)
    4. Extracts the reply and action log for the API response
    """

    def __init__(
        self,
        db: WorkspaceDB,
        chat_model: Any | None = None,
        langfuse_handler: Any | None = None,
        code_executor: Any | None = None,
        search_client: Any | None = None,
    ) -> None:
        self.db = db
        self.model = chat_model or create_chat_model()
        self.langfuse_handler = langfuse_handler
        self.code_executor = code_executor
        self.search_client = search_client

    def handle_message(self, workspace: str, message: str) -> dict[str, Any]:
        text = message.strip()
        if not text:
            raise ValidationError("Chat message must not be empty.")

        from langgraph.prebuilt import create_react_agent

        tools = create_workspace_tools(
            self.db, workspace,
            executor=self.code_executor, search_client=self.search_client,
        )
        agent = create_react_agent(
            self.model,
            tools,
            prompt=REACT_SYSTEM_PROMPT,
        )

        config: dict[str, Any] = {}
        if self.langfuse_handler:
            config["callbacks"] = [self.langfuse_handler]

        try:
            result = agent.invoke(
                {"messages": [HumanMessage(content=text)]},
                config=config,
            )
            return self._parse_result(result)
        except Exception as exc:
            logger.exception("LangGraph agent error for workspace=%s", workspace)
            return {
                "status": "error",
                "reply": f"Agent 执行失败: {exc}",
                "actions": [],
            }

    def handle_message_stream(self, workspace: str, message: str):
        """Yield SSE-friendly dicts as the agent executes each step.

        Uses ``stream_mode="messages"`` for real-time token streaming.

        Event types:
          - ``token``      : a text token from the LLM (stream in real-time)
          - ``tool_call``  : about to call a tool (emitted with full args)
          - ``tool_result``: tool returned
          - ``error``      : something went wrong
          - ``done``       : execution finished (carries full result)
        """
        text = message.strip()
        if not text:
            yield {"event": "error", "data": "Empty message"}
            return

        from langgraph.prebuilt import create_react_agent

        tools = create_workspace_tools(
            self.db, workspace,
            executor=self.code_executor, search_client=self.search_client,
        )
        agent = create_react_agent(
            self.model,
            tools,
            prompt=REACT_SYSTEM_PROMPT,
        )

        config: dict[str, Any] = {}
        if self.langfuse_handler:
            config["callbacks"] = [self.langfuse_handler]

        actions: list[dict[str, Any]] = []
        reply_parts: list[str] = []
        pending_tc: dict[int, dict[str, str]] = {}
        llm_rounds = 0
        in_tool_phase = False

        try:
            for chunk, _metadata in agent.stream(
                {"messages": [HumanMessage(content=text)]},
                config=config,
                stream_mode="messages",
            ):
                if isinstance(chunk, AIMessageChunk):
                    if in_tool_phase:
                        in_tool_phase = False
                        llm_rounds += 1
                        reply_parts.clear()

                    if chunk.content:
                        reply_parts.append(chunk.content)
                        yield {"event": "token", "data": chunk.content}

                    for tc_chunk in chunk.tool_call_chunks or []:
                        idx = tc_chunk.get("index", 0)
                        name = tc_chunk.get("name")
                        args_frag = tc_chunk.get("args", "") or ""
                        if name:
                            pending_tc[idx] = {"name": name, "args": args_frag}
                        elif idx in pending_tc:
                            pending_tc[idx]["args"] += args_frag

                elif isinstance(chunk, ToolMessage):
                    if not in_tool_phase:
                        in_tool_phase = True
                        llm_rounds += 1
                        for tc_info in self._flush_pending_tc(pending_tc, actions):
                            yield {
                                "event": "tool_call",
                                "data": {"tool": tc_info["name"], "args": tc_info["args"]},
                            }

                    content_str = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                    truncated = content_str[:500] + ("..." if len(content_str) > 500 else "")
                    status = "error" if content_str.startswith("Error:") else "ok"
                    tool_name = getattr(chunk, "name", "") or (actions[-1]["tool"] if actions else "")
                    if actions:
                        actions[-1]["summary"]["status"] = status
                    yield {
                        "event": "tool_result",
                        "data": {"tool": tool_name, "status": status, "output": truncated},
                    }

        except Exception as exc:
            logger.exception("LangGraph stream error for workspace=%s", workspace)
            yield {"event": "error", "data": str(exc)}
            return

        reply = "".join(reply_parts)
        yield {
            "event": "done",
            "data": {
                "status": "ok",
                "reply": reply,
                "actions": actions,
                "llm_rounds": llm_rounds,
            },
        }

    @staticmethod
    def _flush_pending_tc(
        pending_tc: dict[int, dict[str, str]],
        actions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drain accumulated tool_call_chunks, append to *actions*, return them."""
        flushed: list[dict[str, Any]] = []
        for idx in sorted(pending_tc):
            tc = pending_tc[idx]
            try:
                args = json.loads(tc["args"]) if tc["args"] else {}
            except (json.JSONDecodeError, TypeError):
                args = {"raw": tc["args"]}
            entry = {"name": tc["name"], "args": args}
            actions.append({"tool": tc["name"], "args": args, "summary": {"status": "ok"}})
            flushed.append(entry)
        pending_tc.clear()
        return flushed

    def _parse_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Extract reply text and action log from LangGraph result messages."""
        messages = result.get("messages", [])
        reply = ""
        actions: list[dict[str, Any]] = []
        llm_rounds = 0

        for msg in messages:
            if isinstance(msg, AIMessage):
                llm_rounds += 1
                if msg.content and not msg.tool_calls:
                    reply = msg.content
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        actions.append({
                            "tool": tc["name"],
                            "args": tc["args"],
                            "summary": {"status": "ok"},
                        })
            elif isinstance(msg, ToolMessage):
                if actions:
                    content_str = msg.content if isinstance(msg.content, str) else str(msg.content)
                    if content_str.startswith("Error:"):
                        actions[-1]["summary"]["status"] = "error"

        if not reply and messages:
            last = messages[-1]
            if isinstance(last, AIMessage) and last.content:
                reply = last.content

        return {
            "status": "ok",
            "reply": reply,
            "actions": actions,
            "llm_rounds": llm_rounds,
        }

    @staticmethod
    def is_configured() -> bool:
        return bool(
            os.environ.get("LLM_API_KEY")
            or os.environ.get("GLM_API_KEY")
            or os.environ.get("ZHIPUAI_API_KEY")
        )
