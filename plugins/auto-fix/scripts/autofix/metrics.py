"""Metrics: one JSONL line per stage per round, plus aggregation.

Raw counters only.  Cost is derived at summary time from the plugin's own
``price_table.toml`` — the CLI's ``total_cost_usd`` is recorded but never used,
because it carries ``costBasis: "unknown"`` and a wrong built-in rate for
third-party models.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import log, paths
from .isolation import Profile
from .proc import ProcResult


@dataclass
class RunContext:
    run_id: str
    repo: str
    issue: int
    plugin_ver: str = "0.1.0"
    cli_ver: str = "unknown"
    self_review: bool = False
    extra: dict = field(default_factory=dict)


_ONE_LINE_MAX = 8192


class MetricsSink:
    """Append-only JSONL writer, safe for concurrent runs."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")

    def emit(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        if len(line.encode()) > _ONE_LINE_MAX:
            log.warn("metrics record exceeds 8 KiB; truncating extras")
            record = dict(record)
            record["extras"] = {"truncated": True}
            line = json.dumps(record, ensure_ascii=False, default=str)
        # Single write under O_APPEND: atomic enough for concurrent appenders.
        os.write(self._fh.fileno(), (line + "\n").encode("utf-8"))

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass


def new_run_id() -> str:
    return f"{int(time.time())}-{os.getpid()}-{os.urandom(3).hex()}"


def usage_from_result(result: dict) -> dict:
    """Extract raw token counters from a ``claude -p`` result object."""
    usage = result.get("usage") or {}
    return {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
        "thinking_tokens": int((usage.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0),
    }


def base_record(
    ctx: RunContext,
    *,
    phase: str,
    round_no: int,
    profile: Profile | None,
    prompt_file: str | None = None,
    prompt_ver: str | None = None,
) -> dict:
    now = time.time()
    return {
        "schema": 1,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "epoch_ms": int(now * 1000),
        "run_id": ctx.run_id,
        "plugin_ver": ctx.plugin_ver,
        "cli_ver": ctx.cli_ver,
        "repo": ctx.repo,
        "issue": ctx.issue,
        "round": round_no,
        "phase": phase,
        "profile": profile.name if profile else None,
        "model": profile.model if profile else None,
        "prompt_file": prompt_file,
        "prompt_ver": prompt_ver,
        "self_review": ctx.self_review,
    }


def phase_record(
    ctx: RunContext,
    *,
    phase: str,
    round_no: int,
    profile: Profile | None,
    proc: ProcResult,
    result: dict | None,
    classification: str,
    prompt_file: str | None = None,
    prompt_ver: str | None = None,
    extras: dict | None = None,
) -> dict:
    rec = base_record(
        ctx,
        phase=phase,
        round_no=round_no,
        profile=profile,
        prompt_file=prompt_file,
        prompt_ver=prompt_ver,
    )
    rec["exit_code"] = proc.exit_code
    rec["timed_out"] = proc.timed_out
    rec["wall_ms"] = proc.wall_ms
    rec["classification"] = classification
    if result:
        rec["session_id"] = result.get("session_id")
        rec["num_turns"] = result.get("num_turns")
        rec["duration_ms"] = result.get("duration_ms")
        rec["duration_api_ms"] = result.get("duration_api_ms")
        rec["ttft_ms"] = result.get("ttft_ms")
        rec["raw"] = usage_from_result(result)
        rec["per_model"] = result.get("modelUsage") or {}
        # Recorded for comparison only; never used to compute cost.
        rec["cli_total_cost_usd"] = result.get("total_cost_usd")
        rec["cli_cost_basis"] = _cost_basis(result)
    rec["extras"] = extras or {}
    return rec


def _cost_basis(result: dict) -> str | None:
    per_model = result.get("modelUsage") or {}
    for entry in per_model.values():
        if isinstance(entry, dict) and entry.get("costBasis"):
            return str(entry["costBasis"])
    return None


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def load_price_table() -> dict:
    import tomllib

    path = paths.price_table_path()
    if not path.exists():
        return {"version": "none", "models": {}}
    try:
        return tomllib.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        log.warn(f"could not read price table: {exc}")
        return {"version": "error", "models": {}}


def compute_cost(raw: dict, model: str | None, table: dict) -> float | None:
    entry = (table.get("models") or {}).get(model or "")
    if not entry:
        return None
    per_m = 1_000_000
    return (
        raw.get("input_tokens", 0) / per_m * entry.get("input", 0)
        + raw.get("output_tokens", 0) / per_m * entry.get("output", 0)
        + raw.get("cache_read_input_tokens", 0) / per_m * entry.get("cache_read", 0)
        + raw.get("cache_creation_input_tokens", 0) / per_m * entry.get("cache_write", 0)
    )


def read_records(files: list[Path]) -> list[dict]:
    records: list[dict] = []
    for path in files:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # A truncated tail from a concurrent append is expected; skip it.
                continue
    return records


def discover_metric_files(*, repo: str | None) -> list[Path]:
    files: list[Path] = []
    base = paths.local_metrics_dir()
    if base.exists():
        for path in sorted(base.glob("*.jsonl")):
            if repo is None or _slug(path.stem) == _slug(repo):
                files.append(path)
    return files


def _slug(repo: str) -> str:
    return repo.replace("/", "__")


def summarize(records: list[dict], *, group_by: tuple[str, ...] = ("phase",)) -> list[dict]:
    table = load_price_table()
    rows: dict[tuple, dict] = {}
    for rec in records:
        key = tuple(rec.get(field) for field in group_by)
        row = rows.setdefault(key, {"key": key, "count": 0, "records": []})
        row["count"] += 1
        row["records"].append(rec)

    out: list[dict] = []
    for row in rows.values():
        recs = row["records"]
        walls = [r["wall_ms"] for r in recs if r.get("wall_ms")]
        costs = []
        tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "thinking": 0}
        for r in recs:
            raw = r.get("raw") or {}
            tokens["input"] += raw.get("input_tokens", 0)
            tokens["output"] += raw.get("output_tokens", 0)
            tokens["cache_read"] += raw.get("cache_read_input_tokens", 0)
            tokens["cache_creation"] += raw.get("cache_creation_input_tokens", 0)
            tokens["thinking"] += raw.get("thinking_tokens", 0)
            cost = compute_cost(raw, r.get("model"), table)
            if cost is not None:
                costs.append(cost)
        denom = tokens["input"] + tokens["cache_read"] + tokens["cache_creation"]
        out.append(
            {
                "key": row["key"],
                "count": row["count"],
                "wall_ms_p50": int(statistics.median(walls)) if walls else 0,
                "wall_ms_p90": int(_percentile(walls, 90)) if walls else 0,
                "tokens": tokens,
                "cache_hit_ratio": round(tokens["cache_read"] / denom, 3) if denom else 0.0,
                "cost_usd": round(sum(costs), 6) if costs else None,
                "unpriced_records": row["count"] - len(costs),
            }
        )
    out.sort(key=lambda r: (-r["wall_ms_p50"], str(r["key"])))
    return out


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round((pct / 100) * len(ordered) + 0.5)) - 1))
    return ordered[idx]


def render_table(rows: list[dict], group_by: tuple[str, ...]) -> str:
    if not rows:
        return "No metrics recorded yet."
    header = [*group_by, "n", "wall p50", "wall p90", "in", "out", "cache_r", "hit%", "cost"]
    lines = [" | ".join(header), "-+-".join("-" * len(h) for h in header)]
    for row in rows:
        t = row["tokens"]
        cells = [
            # Phases like `tests` and `run` have no model; show that as a dash
            # rather than a literal "None".
            *["-" if k is None else str(k) for k in row["key"]],
            str(row["count"]),
            _ms(row["wall_ms_p50"]),
            _ms(row["wall_ms_p90"]),
            str(t["input"]),
            str(t["output"]),
            str(t["cache_read"]),
            f"{row['cache_hit_ratio'] * 100:.0f}",
            "-" if row["cost_usd"] is None else f"${row['cost_usd']:.4f}",
        ]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def _ms(value: int) -> str:
    if value >= 60_000:
        return f"{value / 60_000:.1f}m"
    return f"{value / 1000:.1f}s"
