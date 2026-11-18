"""
Swarm engine -- minimal handoff loop + parallel-section generator.

The engine drives a sequence of *agents* (LLM role definitions).
Each agent:
  1. Receives its system prompt plus conversation history.
  2. May call tools via OpenAI tool-calling format.
  3. Returns its result string and the next agent to hand off to, or ``None`` to end.

Parallel sections are discovered during initial intent classification and
executed with ``asyncio.gather`` for speed.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import httpx
from openai import AsyncOpenAI

from .prompts import AGENT_PROMPTS, SYSTEM_GUARDRAILS, classify_prompt
from .tools import (
    ToolRegistry,
)

# --------------------------------------------------------------------------- #
# Enums & models                                                              #
# --------------------------------------------------------------------------- #


class Agent(str, Enum):
    ROUTE = "route"
    ZOHOSWARM = "zohe_swarm"
    JIRASWARM = "jira_swarm"
    GMAILSWARM = "gmail_swarm"
    WORKSWARM = "work_swarm"


@dataclass
class SwarmSession:
    session_id: str
    caller: str
    messages_api: list[dict] = field(default_factory=list)
    current_agent: Optional[Agent] = None
    tool_registry: ToolRegistry | None = None
    loop_count: int = 0
    max_loops: int = 15
    ended: bool = False
    result: str = ""


@dataclass
class _ParGroup:
    """A group of agents that should run in parallel."""

    agents: List[Agent]
    task: str


# --------------------------------------------------------------------------- #
# LLM client helper                                                           #
# --------------------------------------------------------------------------- #


def _make_llm() -> AsyncOpenAI:
    from os import environ as _osenv

    base = _osenv.get("LLM_BASE_URL", "http://localhost:11434/v1")
    key = _osenv.get("LLM_API_KEY", "ollama")
    return AsyncOpenAI(base_url=base, api_key=key)


def _make_fast_llm() -> AsyncOpenAI:
    from os import environ as _osenv

    base = _osenv.get("LLM_BASE_URL", "http://localhost:11434/v1")
    key = _osenv.get("LLM_API_KEY", "ollama")
    return AsyncOpenAI(base_url=base, api_key=key)


# --------------------------------------------------------------------------- #
# JSON / markdown fence extraction                                            #
# --------------------------------------------------------------------------- #


def _extract_json(text: str) -> Any:
    """Try to extract usable JSON from model output.

    Local models sometimes wrap the JSON in markdown code fences or add
    trailing text.  We handle both cases here.
    """
    # Strip markdown fence wrapper
    fenced = re.search(r"```(?:json)?\s*(.*?)\n?```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Last resort -- grab the outermost braces / brackets
    brace_stack: List[str] = []
    bracket_stack: List[str] = []
    start_brace = start_bracket = -1
    open_quote = False

    for i, ch in enumerate(text):
        if ch == '"' and (i == 0 or text[i - 1] != "\\"):
            open_quote = not open_quote
        if open_quote:
            continue
        if ch in "{[":
            if start_brace == -1 and ch == "{":
                start_brace = i
            if start_bracket == -1 and ch == "[":
                start_bracket = i
            brace_stack.append(ch)
            bracket_stack.append(ch)
        elif ch in "}]":
            brace_stack.pop()
            bracket_stack.pop()
            stack_map = {"{": "}", "[": "]"}
            if stack_map[brace_stack[-1]] != ch:
                return None  # malformed

    # Return the last balanced slice
    for length in range(len(text), 0, -1):
        slice_ = text[:length]
        try:
            json.loads(slice_)
            return json.loads(slice_)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


# --------------------------------------------------------------------------- #
# Intent classifier (uses a *fast* model)                                     #
# --------------------------------------------------------------------------- #

_PARALLEL_PATTERN = re.compile(
    r"(?P<agents>(?:[\w]+(?:\s*,?\s*))+)".replace(r"\w+", r"[a-zA-Z_]\w*"),
)


def _classify_for_parallel(query: str, llm: AsyncOpenAI) -> List[_ParGroup]:
    """Ask the fast LLM whether independent sub-tasks exist."""
    prompt = classify_prompt.format(query=query)
    try:
        resp = llm.chat.completions.create(
            model=_get_fast_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
        )
        parsed = _extract_json(resp.choices[0].message.content or "")
    except Exception:
        return []

    # Normalise the response (flexible schema)
    if isinstance(parsed, dict):
        groups_raw = parsed.get("parallel_groups", parsed.get("groups", [{}]))
    else:
        groups_raw = [parsed] if isinstance(parsed, list) else []

    groups: List[_ParGroup] = []
    for g in groups_raw:
        agents_str = (g.get("agents") or g.get("name", "")).replace(
            " ", ""
        ).split(",")
        agents_named = [a.strip().upper() for a in agents_str if a.strip()]
        task = g.get("task", "")
        if agents_named and task:
            groups.append(_ParGroup(agents=agents_named, task=task))

    return groups


def _get_fast_model() -> str:
    from os import environ as _osenv
    return _osenv.get("FAST_LLM_MODEL", "qwen3.6")


# --------------------------------------------------------------------------- #
# Tool registration & dispatch                                                #
# --------------------------------------------------------------------------- #

def _get_agent_tools(agent: Agent) -> tuple[list[dict[str, Any]], dict[str, Callable[..., Any]]]:
    """Return (tool_schemas, dispatch_map) for a given agent.

    Schema dicts follow OpenAI ``function`` calling format.  The dispatch map
    maps *function name* to an async callable that accepts ``(name: str, args: dict)``.
    """
    # Import tool modules lazily to avoid circular deps at module load time
    from . import tools

    if agent in (Agent.ZOHOSWARM, Agent.ROUTE):
        s1 = tools.zoho_tool_schemas()[0]
        s2 = tools.zoho_tool_schemas()[1]
        schemas: list[dict[str, Any]] = [s1, s2]  # type: ignore[list-item]
        dispatch: dict[str, Callable[..., Any]] = {
            "zoho_search_contacts": lambda name, args: tools.zoho_tool_call(name, args),  # noqa: E731
            "zoho_crud_record": lambda name, args: tools.zoho_tool_call(name, args),  # noqa: E731
        }
        return schemas, dispatch

    if agent == Agent.JIRASWARM:
        s1 = tools.jira_tool_schemas()[0]
        s2 = tools.jira_tool_schemas()[1]
        schemas = [s1, s2]  # type: ignore[list-item]
        dispatch = {
            "jira_create_ticket": lambda name, args: tools.jira_tool_call(name, args),  # noqa: E731
            "jira_query": lambda name, args: tools.jira_tool_call(name, args),  # noqa: E731
        }
        return schemas, dispatch

    if agent == Agent.GMAILSWARM:
        s1 = tools.gmail_tool_schemas()[0]
        schemas = [s1]  # type: ignore[list-item]
        dispatch = {
            "gmail_send_message": lambda name, args: tools.gmail_tool_call(name, args),  # noqa: E731
        }
        return schemas, dispatch

    if agent == Agent.WORKSWARM:
        schemas: list[dict[str, Any]] = []
        dispatch: dict[str, Callable[..., Any]] = {}
        for schema_fn, impl in [
            (tools.graph_tool_schemas, tools.graph_tool_call),
            (tools.whatsapp_tool_schemas, tools.whatsapp_tool_call),
        ]:
            try:
                for s in schema_fn():
                    schemas.append(s)  # type: ignore[arg-type]
                    dispatch[s["function"]["name"]] = lambda n, a, i=impl: i(n, a)  # noqa: E731
            except Exception:
                pass

        # Add sqlite tool separately (schema in tools.py, impl is local)
        try:
            for s in tools.sqlite_tool_schemas():
                schemas.append(s)  # type: ignore[arg-type]
        except Exception:
            pass
        if schemas and any(s["function"]["name"] == "sqlite_query" for s in schemas):
            dispatch["sqlite_query"] = lambda n, a: sqlite_query_wrapper(a.get("query", "").strip())  # noqa: E731

        return schemas, dispatch

    return [], {}


# --------------------------------------------------------------------------- #
# Agent runner                                                                #
# --------------------------------------------------------------------------- #

async def _run_agent(
    agent: Agent,
    session: SwarmSession,
    user_query: str,
) -> tuple[str, Optional[Agent], List[Any]]:
    """Run a single agent pass -> return (result_text, next_agent, tool_results)."""
    registry = session.tool_registry or ToolRegistry()

    # Build messages for this agent.
    system_prompt = AGENT_PROMPTS.get(agent, SYSTEM_GUARDRAILS)

    messages = [{"role": "system", "content": f"{SYSTEM_GUARDRAILS}\n\n{system_prompt}"}]

    # Prepend conversation history from previous agent passes (sanitized).
    for m in session.messages_api:
        if isinstance(m.get("content"), str) and len(m["content"]) < 8192:
            messages.append({k: v for k, v in m.items() if k in ("role", "content")})

    # Add the current user turn.
    messages.append({"role": "user", "content": user_query})

    # --- Get tools & dispatchers for this agent ------------------------------
    tool_schemas, tool_dispatch = _get_agent_tools(agent)

    # --- Call LLM with tools ---------------------------------------------------
    llm = _make_llm()
    model = _get_model_from_env()

    extra: dict[str, Any] = {}
    if tool_schemas:
        extra["tools"] = tool_schemas
        extra["tool_choice"] = "auto"

    resp = await llm.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.15,
        max_tokens=4096,
        **extra,  # type: ignore[arg-type]
    )

    message_resp = resp.choices[0].message
    tool_calls = getattr(message_resp, "tool_calls", None) or []

    # --- Tool call round-robin -------------------------------------------------
    while tool_calls:
        new_tool_results: List[Any] = []

        for tc in tool_calls:
            fn_name = tc.function.name
            raw_args = tc.function.arguments or "{}"
            fn_args = _extract_json(raw_args)
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except (json.JSONDecodeError, ValueError):
                    fn_args = {}

            # Look up the appropriate dispatch function for this tool name
            result_text = "no handler"
            if fn_name in tool_dispatch:
                try:
                    raw_result = tool_dispatch[fn_name](fn_name, fn_args or {})
                    # Handle both sync and awaitable results
                    if hasattr(raw_result, "__await__"):
                        result_text = str(await raw_result)  # type: ignore[misc]
                    else:
                        result_text = str(raw_result)
                except Exception as exc:
                    result_text = f"tool_error:{exc}"

            new_tool_results.append(result_text)

            # Append tool result back to conversation.
            session.messages_api.append({
                "role": "assistant",
                "content": message_resp.content or "",
                "tool_calls": [{"id": tc.id, "function": {"name": fn_name, "arguments": raw_args}}],
            })
            session.messages_api.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_text,
            })

        # Re-call model with tool results appended to session history.
        recent = [
            m for m in session.messages_api[-20:]
            if isinstance(m, dict) and "role" in m
            and len(str(m.get("content", ""))) < 8192
        ]

        resp = await llm.chat.completions.create(
            model=model,
            messages=recent,
            temperature=0.15,
            max_tokens=4096,
            tools=tool_schemas if tool_schemas else [],  # type: ignore[arg-type]
            tool_choice="auto" if tool_schemas else None,  # type: ignore[arg-type]
        )

        message_resp = resp.choices[0].message
        tool_calls = getattr(message_resp, "tool_calls", None) or []

    result_text = (message_resp.content or "").strip()

    # Extract next agent hint from the response.
    next_agent: Optional[Agent] = _extract_next_agent(result_text)

    return result_text, next_agent  # type: ignore[return-value]


def _extract_next_agent(text: str) -> Optional[Agent]:
    """Parse ``next_agent: <name>`` from agent output."""
    m = re.search(r"next_agent[:\s]+(\w+)", text, re.IGNORECASE)
    if not m:
        return None
    for a in Agent:
        if m.group(1).lower().replace("_", "") == a.name.lower().replace("_", ""):
            return a
    return None


def sqlite_query_wrapper(query: str, params: list | None = None) -> str:
    """Read-only SQLite SELECT executed against the swarm DB."""
    import sqlite3 as _sqlite

    from ..app.db import get_db_path
    path = get_db_path()
    if not path.exists():
        return "No database file available."
    conn = _sqlite.connect(str(path))
    try:
        conn.row_factory = _sqlite.Row
        rows = conn.execute(query, params or []).fetchall()
        cols = rows[0].keys() if rows else []
        lines = [", ".join(f"{c}={r[c]}" for c in cols) for r in rows]
        return "\n".join(lines) or "Query returned no rows."
    except Exception as exc:
        return f"sqlite_err:{exc}"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Swarm orchestrator                                                          #
# --------------------------------------------------------------------------- #


async def run_swarm(
    user_query: str,
    caller: str = "web",
) -> dict[str, Any]:
    """Run the full swarm handoff loop.

    Returns ``{session_id, result, agents_run, wall_seconds}``.
    """
    session = SwarmSession(
        session_id=f"ses_{int(time.time())}_{time.monotonic_ns()}",
        caller=caller,
    )
    session.messages_api.append({"role": "user", "content": user_query})

    t_start = time.time()
    agents_run: List[str] = []

    # --- Check for HITL approvals that might short-circuit things ---------------
    from ..app.db import hitl_get_pending, hitl_update

    pending = hitl_get_pending(caller)
    if pending:
        session.messages_api.append({
            "role": "system",
            "content": f"Pending HITL approvals (auto-resolved as denied): {';'.join(p.id for p in pending)}",
        })
        for h in pending:
            hitl_update(h.id, "denied", answerer=caller)

    # --- Attempt parallel split ------------------------------------------------
    fast_llm = _make_fast_llm()
    par_groups = _classify_for_parallel(user_query, fast_llm)
    parallel_results: Dict[str, str] = {}

    if par_groups and len(par_groups) > 1:
        for grp in par_groups:
            # Run each group sequentially (within-group sequential).
            sub_agent = Agent(grp.agents[0]) if grp.agents else Agent.ZOHOSWARM
            result, next_a, _ = await _run_agent(sub_agent, session, f"(parallel) {grp.task}")
            agents_run.append(sub_agent.value)
            parallel_results[sub_agent.value] = result

        # Merge parallel results and do a final synthesis pass.
        merged_prompt = (
            "I received results from several parallel task groups.\n"
            + "\n---\n".join(f"{k}: {v}" for k, v in parallel_results.items())
        )
        session.messages_api.append({"role": "system", "content": f"ParallelGroupResult:\n{merged_prompt}"})
        final_result, next_a, _ = await _run_agent(Agent.WORKSWARM, session, "Synthesise results from all groups.")
        agents_run.append("synthesis")
        session.loop_count = 1
    else:
        # --- Sequential handoff loop -----------------------------------------
        current = Agent.ROUTE
        while not session.ended and session.loop_count < session.max_loops:
            result, next_a, _ = await _run_agent(current, session, user_query)

            if next_a is None or not result.strip():
                # Termination signal from the agent.
                session.result = result
                session.ended = True
                break

            agents_run.append(current.value)
            user_query = f"[{current.value}]\n\n{result}"
            current = next_a

    session.messages_api.append({"role": "system", "content": "_END"})
    duration = time.time() - t_start

    return {
        "session_id": session.session_id,
        "result": session.result or user_query,
        "agents_run": agents_run,
        "wall_seconds": round(duration, 2),
    }


def _get_model_from_env() -> str:
    """Resolve the model name for full-resolution calls."""
    from os import environ as _osenv

    return _osenv.get("LLM_MODEL", "qwen3.6")
