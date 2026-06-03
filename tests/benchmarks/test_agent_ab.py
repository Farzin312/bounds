"""Unit tests for the real-agent A/B harness's pure parsing — no network, no `claude` CLI.

Locks the stream-json contract `agent_ab._parse_stream` depends on: the final `result` event
carries usage/cost/turns, assistant `tool_use` blocks are counted (and Bash-with-"bounds" calls
counted separately), and the answer is recovered from `result` (or the last assistant text).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[2] / "benchmarks" / "agent_ab.py"
    spec = importlib.util.spec_from_file_location("agent_ab_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events)


def test_parse_stream_extracts_metrics_tools_and_bounds_calls():
    agent_ab = _load()
    stdout = _stream(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "bounds where Profile"}},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "Profile"}},
            {"type": "text", "text": "thinking..."},
        ]}},
        {"type": "result", "result": "ANSWER: Profile is in apps/mcp/src/client.ts",
         "total_cost_usd": 0.07, "duration_ms": 12345, "num_turns": 3, "is_error": False,
         "modelUsage": {"claude-sonnet-4-5": {}},
         "usage": {"input_tokens": 10, "output_tokens": 20,
                   "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 500}},
    )
    metrics, tool_calls, bounds_calls, answer = agent_ab._parse_stream(stdout)
    assert tool_calls == 2
    assert bounds_calls == 1  # only the Bash(bounds ...) call
    assert metrics["total_tokens"] == 10 + 20 + 1000 + 500
    assert metrics["cost_usd"] == 0.07
    assert metrics["num_turns"] == 3
    assert metrics["model"] == "claude-sonnet-4-5"
    assert "apps/mcp/src/client.ts" in answer


def test_parse_stream_without_bounds_counts_no_bounds_calls():
    agent_ab = _load()
    stdout = _stream(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "a.ts"}},
        ]}},
        {"type": "result", "result": "done", "total_cost_usd": 0.01, "duration_ms": 1,
         "num_turns": 1, "usage": {"input_tokens": 1, "output_tokens": 1,
                                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    )
    _, tool_calls, bounds_calls, _ = agent_ab._parse_stream(stdout)
    assert tool_calls == 2
    assert bounds_calls == 0


def test_parse_stream_falls_back_to_last_text_when_no_result_text():
    agent_ab = _load()
    stdout = _stream(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "the answer"}]}},
        {"type": "result", "result": "", "total_cost_usd": 0.0, "duration_ms": 1, "num_turns": 1,
         "usage": {"input_tokens": 0, "output_tokens": 0,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    )
    _, _, _, answer = agent_ab._parse_stream(stdout)
    assert answer == "the answer"


def test_is_bounds_cmd_matches_invocation_not_substring():
    agent_ab = _load()
    assert agent_ab._is_bounds_cmd("bounds describe mcp-src") is True
    assert agent_ab._is_bounds_cmd("  bounds where Profile") is True
    assert agent_ab._is_bounds_cmd("/Users/x/.venv/bin/bounds list") is True
    # substring / unrelated commands must NOT count as a bounds invocation
    assert agent_ab._is_bounds_cmd("grep -r bounds .") is False
    assert agent_ab._is_bounds_cmd("cat out-of-bounds.ts") is False
    assert agent_ab._is_bounds_cmd("") is False


def test_parse_stream_handles_garbage_lines():
    agent_ab = _load()
    metrics, tool_calls, bounds_calls, answer = agent_ab._parse_stream("not json\n\n{bad}\n")
    assert metrics == {} and tool_calls == 0 and bounds_calls == 0 and answer == ""
