#!/usr/bin/env python3
"""A fake `claude` CLI for testing the orchestrator without spending tokens.

It reads the same argv surface the plugin uses and replays a scripted scenario
from ``FAKE_CLAUDE_SCRIPT`` (a JSON file).  Each invocation pops the next step.

A step looks like:

    {"phase": "fixer", "edit": {"path": "calc.py", "content": "..."},
     "usage": {...}, "structured": {...}, "exit": 0}

For a reviewer step, ``structured`` is returned as ``structured_output``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    script_path = os.environ.get("FAKE_CLAUDE_SCRIPT")
    counter_path = os.environ.get("FAKE_CLAUDE_COUNTER")

    # `--version` is a probe, not a scenario step: it must never consume one.
    if "--version" in sys.argv:
        print("2.1.263 (fake)")
        return 0

    if script_path is None:
        print("fake claude: no FAKE_CLAUDE_SCRIPT set", file=sys.stderr)
        return 1

    steps = json.loads(Path(script_path).read_text())
    idx = 0
    if counter_path and Path(counter_path).exists():
        idx = int(Path(counter_path).read_text() or "0")
    if counter_path:
        Path(counter_path).write_text(str(idx + 1))

    if idx >= len(steps):
        print(f"fake claude: script exhausted at call {idx}", file=sys.stderr)
        return 1
    step = steps[idx]

    # Apply a file edit, mimicking what the fixer would leave in the tree.
    for edit in step.get("edits", []):
        path = Path(edit["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(edit["content"])

    # Write a structured verdict to stdout the way `-p --output-format json` does.
    sys.stdin.read()  # consume the prompt
    usage = step.get(
        "usage",
        {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 0,
            "output_tokens_details": {"thinking_tokens": 0},
        },
    )
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": step.get("exit", 0) != 0,
        "exit_code": step.get("exit", 0),
        "duration_ms": step.get("duration_ms", 1234),
        "duration_api_ms": step.get("duration_api_ms", 1000),
        "ttft_ms": step.get("ttft_ms", 200),
        "num_turns": step.get("num_turns", 3),
        "session_id": f"fake-{idx}",
        "total_cost_usd": 0.0,
        "usage": usage,
        "modelUsage": {
            step.get("model", "fake-model"): {
                "inputTokens": usage["input_tokens"],
                "outputTokens": usage["output_tokens"],
                "contextWindow": 200000,
                "costBasis": "unknown",
            }
        },
        "result": step.get("result", "done"),
    }
    if "structured" in step:
        result["structured_output"] = step["structured"]
    print(json.dumps(result))
    return step.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main())
