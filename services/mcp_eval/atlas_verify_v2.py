#!/usr/bin/env python3
"""Deterministic run-quality verifier for MCP-Atlas evaluations.

Design decisions, and why they differ from ``altas-verfied.py``:

1. **The denominator never moves.** The headline score is always
   ``pass / total``. The old script removed "environment error" tasks from the
   denominator, which made every model's score computed over a different task
   subset -- and rewarded runs that happened to hit a worse environment (a run
   with 76 removals gained +11 points, one with 61 gained +6.9). Environment
   damage is reported as a separate *upper bound*, never folded into the score.

2. **Rules, not a judge.** Every classification below is a literal substring
   match against the tool's own error envelope. The old LLM judge compared the
   trajectory against the reference answer, so it labelled "tool returned fine
   but the content differs from the reference" as an environment error (mongodb
   was flagged on 17 tasks while its real failure rate was 0.5%), and it
   contradicted itself -- writing "this is a parameter error, not a tool runtime
   error" in the reason field while still emitting the environment label.

3. **Model mistakes stay model mistakes.** Sandbox path rejections, parameter
   type errors, and robots.txt refusals are the model choosing badly, not the
   environment breaking. They are counted, and explicitly never credited.

4. **Corroboration across runs.** A tool that fails in every run is broken
   infrastructure; one that fails in a single run is that model's behaviour.
   Pass several scored CSVs and the report separates the two.

5. **Infrastructure failures are their own class.** A task whose trajectory is
   empty after exhausting retries never reached the model. That is neither a
   model error nor an MCP error, and it is the single largest score distortion
   in practice -- so it gates run validity instead of being silently scored 0.

6. **An environment error only counts if it blocked the task.** Earlier versions
   called a failure "environment-touched" when *any* call in the trajectory hit
   an environment rule, which over-counted by 2-3x on every run audited: one
   aviation task chased 15 dead URLs the model invented, two of which failed on
   robots.txt, and the whole task was credited to the environment. A signature
   is now *blocking* only when no later call to the same server returned
   substantive content in that task. Both numbers are reported -- ``blocking``
   drives the ceiling, ``touched`` stays for continuity with old reports.

7. **The validity gate ignores model-caused failures.** A run was marked INVALID
   because "weather-data-weather" failed 27/27 calls -- but that is not a server,
   it is what ``name.split("_")[0]`` yields when a model writes
   ``weather-data-weather_astronomy`` instead of ``weather-data_weather_astronomy``.
   The gate now only counts failures no MODEL_RULE claimed, and only for server
   names the run's ``ENABLED_TOOLS`` actually contain.

8. **Per-tool outages are reported.** ``weather_history`` failed 16/16 calls while
   its server sat at 19% because ``weather_astronomy`` and ``weather_search`` were
   healthy. Server-level rates hide tool-level outages, so tools are ranked too.

9. **The other two logs carry evidence the CSV cannot.** Upstream status codes
   live in ``mcp_usage_log`` -- without them ``Error: 'job'`` is unattributable
   and a 20-minute window where one shared key returned 35x HTTP 401 across
   three servers is invisible. Call durations and rate-limit queue waits live in
   ``runtime_logs/tools_*.jsonl``, which is the only place a 300s queue wait
   behind a concurrency-1 gate shows up. Pass ``--usage-log``/``--runtime-log``.

Usage::

    python3 atlas_verify_v2.py --scored evaluation_results/scored_<model>.csv
    python3 atlas_verify_v2.py --scored a.csv --scored b.csv --corroborate
    python3 atlas_verify_v2.py --scored x.csv --eval-log completion_results/mcp_eval.log \
        --runtime-log completion_results/runtime_logs --usage-log ../../mcp_usage_log
"""

from __future__ import annotations

import argparse
import ast
import collections
import csv
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

csv.field_size_limit(sys.maxsize)

PASS_THRESHOLD = 0.75

# --------------------------------------------------------------------------
# Classification rules.
#
# Both error envelopes are matched: sandbox images before 2026-07-27 emit
# `{"detail":"Failed to call tool 'X': ..."}` and newer ones emit
# `{"detail":"Tool 'X' execution failed: ..."}`. A scan that only knows one
# form silently under-reports every older run.
# --------------------------------------------------------------------------

FAILURE_ENVELOPES = (
    "execution failed",
    "Failed to call tool",
    "Error executing tool",
    "Tool execution failed",
    "timed out after",
    "Rate limit exceeded",
    # The oxylabs wrapper raises through httpx, so an upstream 401 arrives as
    # `HTTP error during POST request: 401 - {...}` and a client timeout as
    # `Request error during POST request:` with an empty message. Neither used
    # to match, so 20 authentication failures were counted as *successful*
    # calls and the server showed 0 failures in the health table.
    "HTTP error during POST request",
    "Request error during POST request",
    # Older harness builds reject an unknown tool name with this instead of
    # "Tool is not enabled for task".
    "Unknown tool:",
    # exa reports its own status inline: `web_search_exa error (401): [object Object]`.
    "error (401)",
    "error (429)",
)

# Infrastructure is at fault: the model could not have succeeded.
ENV_RULES: list[tuple[str, str]] = [
    ("auth_rejected", "Bad credentials"),
    ("auth_rejected", "Unauthorized"),
    # The shared gateway rejects a spent key with this body, and it is the only
    # marker three different servers (brave, exa, oxylabs) have in common when
    # the one key they share stops working. The parenthesised request id has to
    # be part of the needle: a bare "Invalid token" also matches git's revision
    # parser saying `Invalid token: '.'`, which has nothing to do with a key.
    ("auth_rejected", "Invalid token (request id"),
    ("auth_rejected", "POST request: 401"),
    ("auth_rejected", "error (401)"),
    # An expired Google refresh token surfaces here, not as "Unauthorized".
    ("auth_rejected", "invalid_grant"),
    ("quota_exhausted", "API key is limited"),
    ("quota_exhausted", "quota"),
    ("quota_exhausted", "exceeded your"),
    ("rate_limited", "Rate limit exceeded"),
    ("rate_limited", "Too Many Requests"),
    ("rate_limited", "HTTP 429"),
    # fetch words the same condition as "status code 429".
    ("rate_limited", "status code 429"),
    ("rate_limited", "error (429)"),
    ("upstream_down", "Service Unavailable"),
    ("upstream_down", "Service Temporarily Unavailable"),
    ("upstream_down", "Cannot connect to host"),
    ("upstream_down", "due to a connection issue"),
    ("upstream_5xx", "status code 5"),
    # arxiv words it as "resulted in HTTP 503", which "status code 5" misses.
    ("upstream_5xx", "HTTP 500"),
    ("upstream_5xx", "HTTP 502"),
    ("upstream_5xx", "HTTP 503"),
    ("upstream_5xx", "HTTP 504"),
    ("tool_timeout", "timed out after"),
    ("server_defect", "has no attribute"),
    ("server_defect", "Output validation error"),
    ("server_defect", "Invalid response shape"),
    ("server_defect", "Failed to initialize server session"),
    ("sandbox_missing_dep", "ModuleNotFoundError"),
    # `Error: 'job'` is a KeyError from the oxylabs wrapper, which reads
    # response_json['job'] *before* raise_for_status(). Any non-2xx body without
    # a 'job' key lands here, so the real status is destroyed: the four
    # occurrences in one run were upstream 400s (three Google Scholar URLs and
    # one `file:///data`), i.e. mostly the model's own bad input. Named for what
    # it is -- lost evidence -- and resolvable only by joining --usage-log.
    ("masked_error", "Error: 'job'"),
]

# Signatures that say a call failed but not why. They are reported separately so
# nobody reads them as proof of an environment fault.
UNATTRIBUTABLE = ("masked_error",)

# Task-local containers are started with no credentials at all -- their only env
# is ENABLED_SERVERS plus a mongo socket path (see
# mcp_completion.tool_policy.TASK_LOCAL_SERVERS). Since the isolated containers
# were given outbound networking, code the *model* wrote can reach the internet
# from them, so an auth, quota, or rate-limit message in their output describes
# the model's own unauthenticated request, never a credential of ours failing.
# Left unscoped, one task's `HTTP Error 401: Unauthorized` -- raised by urllib
# inside model-authored code -- was reported as an environment auth fault.
CREDENTIAL_FREE_SERVERS = frozenset(
    {
        "cli-mcp-server",
        "desktop-commander",
        "filesystem",
        "git",
        "mcp-code-executor",
        "mcp-server-code-runner",
        "memory",
        "mongodb",
    }
)

# e2b-server is deliberately absent: it runs code in E2B's cloud using *our* key,
# so its "Rate limit exceeded" really is our shared account being throttled.
CREDENTIAL_CATEGORIES = frozenset({"auth_rejected", "quota_exhausted", "rate_limited"})

# Checked before ENV_RULES. Each of these matched an environment rule while the
# evidence said otherwise, and each was confirmed against calls that succeeded
# in the same run:
#
#   clinicaltrialsgov wraps upstream 400s in its own SERVICE_UNAVAILABLE code.
#   The same tool answered 44 of 54 calls; the failures used ISO dates in
#   AREA[StartDate]RANGE[...] where the working ones used MM/DD/YYYY.
#
#   desktop-commander reports "no match" and "timed out" in one string, so a
#   search that simply found nothing looked like a stalled tool. The two tasks
#   that hit it had already listed /data successfully -- the files they searched
#   for were never there under those names.
ENV_FALSE_POSITIVES: tuple[str, ...] = (
    "No matches found or search timed out",
)

ENV_FALSE_POSITIVE_PAIRS: tuple[tuple[str, str], ...] = (
    ("SERVICE_UNAVAILABLE", "status 4"),
)

# The model is at fault. Counted, never credited.
MODEL_RULES: list[tuple[str, str]] = [
    ("bad_arguments", "Input validation error"),
    ("bad_arguments", "Invalid arguments for tool"),
    ("bad_arguments", "validation error for"),
    ("sandbox_path_denied", "Access denied - path outside"),
    ("sandbox_path_denied", "Path not allowed"),
    ("robots_refused", "robots.txt"),
    # The model asked for a tool it was not given. Both harness wordings.
    ("unknown_tool", "is not enabled for task"),
    ("unknown_tool", "Unknown tool:"),
    # A relative repo_path resolves against the git server's own install
    # directory, so `.` becomes /agent-environment. The model must pass an
    # absolute path; the environment could also stop offering the trap.
    ("relative_path", "/agent-environment"),
    ("write_tool_blocked", "Cloud account write tool is disabled"),
]

# Empty search results are never auto-classified. A broken server and a query
# that genuinely matches nothing produce byte-identical output, and no rule
# separates them reliably: wikipedia returning nothing for "Chester Bennington"
# is a real fault, while notion returning nothing for "apartment" is Notion's
# title-only search behaving correctly (the same tool answers "real estate").
# They are surfaced for human review with the per-tool ratios attached, and
# excluded from the score, the ceiling, and the validity gate.
EMPTY_RESULT_RE = re.compile(r'"results"\s*:\s*\[\s*\]|"papers"\s*:\s*\[\s*\]')

# e2b's *successful* run_code response is `{"results": [], "logs": {"stdout": [...]}}`
# -- the payload is in logs, results is empty by design. Matching the regex alone
# reported 89 of 93 e2b calls as empty results.
_LOGS_PAYLOAD_RE = re.compile(r'"logs"\s*:\s*\{')


def _is_empty_result(result: str) -> bool:
    if not EMPTY_RESULT_RE.search(result):
        return False
    return not _LOGS_PAYLOAD_RE.search(result)


# A later call "recovered" only if it came back with real content. A two-line
# `[]` or a bare error string is not recovery, so short and empty replies do not
# clear a blocking environment fault.
SUBSTANTIVE_MIN_CHARS = 200


def _is_substantive(result: str) -> bool:
    return (
        not _is_failure(result)
        and not _is_empty_result(result)
        and len(result.strip()) >= SUBSTANTIVE_MIN_CHARS
    )

# Infra failure causes worth naming in the report, matched against the eval log.
INFRA_LOG_RULES: list[tuple[str, str]] = [
    ("fixture_image_missing", "No such image"),
    ("docker_timeout", "Command timed out after"),
    ("docker_start_failed", "failed to create task for container"),
    ("sandbox_no_tools", "list-tools returned no tools"),
    ("model_gateway", "Pangu Response status code"),
]


@dataclass
class ToolCall:
    server: str
    name: str
    result: str
    arguments: str = ""


@dataclass
class TaskRecord:
    task_id: str
    coverage: float
    total_claims: int
    empty_trajectory: bool
    num_retry: int
    calls: list[ToolCall] = field(default_factory=list)
    env_hits: set[str] = field(default_factory=set)
    env_blocking_hits: set[str] = field(default_factory=set)
    model_hits: set[str] = field(default_factory=set)
    empty_result_servers: set[str] = field(default_factory=set)
    enabled_tools: set[str] = field(default_factory=set)

    @property
    def passed(self) -> bool:
        return self.coverage >= PASS_THRESHOLD

    @property
    def enabled_servers(self) -> set[str]:
        return {tool.split("_")[0] for tool in self.enabled_tools}

    def recovered(self, server: str) -> bool:
        """True if ``server`` returned real content after its first failure here.

        This is the whole difference between "an environment error appeared" and
        "an environment error stopped the model". Ordering matters: a success
        *before* the failure proves nothing, because the model still lost the
        call it went on to need.
        """
        failed = False
        for call in self.calls:
            if call.server != server:
                continue
            if _is_failure(call.result):
                failed = True
            elif failed and _is_substantive(call.result):
                return True
        return False


def _parse_history(raw: str) -> list[dict[str, Any]]:
    """Trajectories are JSON, except in generator output where they are repr()."""
    if not raw or not raw.strip():
        return []
    for loader in (json.loads, ast.literal_eval):
        try:
            value = loader(raw)
        except Exception:
            continue
        if isinstance(value, list):
            return value
    return []


def _iter_calls(history: list[dict[str, Any]]) -> Iterator[ToolCall]:
    """Yield each tool call paired with the message that answered it."""
    for index, message in enumerate(history):
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        for offset, call in enumerate(message["tool_calls"]):
            function = call.get("function") or {}
            name = function.get("name") or ""
            arguments = function.get("arguments") or ""
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            reply_index = index + offset + 1
            if reply_index >= len(history):
                continue
            content = history[reply_index].get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            yield ToolCall(
                server=name.split("_")[0],
                name=name,
                result=content,
                arguments=arguments,
            )


def _is_failure(result: str) -> bool:
    head = result[:400]
    return any(marker in head for marker in FAILURE_ENVELOPES) or head.startswith("Error:")


def _is_env_false_positive(result: str) -> bool:
    """True when a message matches an environment rule but is not a fault."""
    head = result[:600]
    if any(marker in head for marker in ENV_FALSE_POSITIVES):
        return True
    return any(a in head and b in head for a, b in ENV_FALSE_POSITIVE_PAIRS)


def env_category(result: str, server: str | None = None) -> str | None:
    """The environment fault this error represents, or None.

    ``server`` is optional for backwards compatibility, but pass it: without it
    a credential category cannot be attributed, and the model's own
    unauthenticated HTTP call from a credential-free container reads as our key
    failing.
    """
    if _is_env_false_positive(result):
        return None
    head = result[:600]
    for category, needle in ENV_RULES:
        if needle in head:
            if (
                server in CREDENTIAL_FREE_SERVERS
                and category in CREDENTIAL_CATEGORIES
            ):
                return None
            return category
    return None


def _enabled_tool_names(raw: str) -> set[str]:
    """``ENABLED_TOOLS`` is a JSON list of names, but older rows hold objects."""
    try:
        value = json.loads(raw or "[]")
    except Exception:
        return set()
    names: set[str] = set()
    for item in value:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict):
            name = item.get("name") or (item.get("function") or {}).get("name")
            if name:
                names.add(name)
    return names


def load_run(path: Path) -> list[TaskRecord]:
    records: list[TaskRecord] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            raw = row.get("raw_conversation_history") or ""
            history = _parse_history(raw)
            try:
                retry = int(row.get("num_retry") or 0)
            except ValueError:
                retry = 0
            record = TaskRecord(
                task_id=row["TASK"],
                coverage=float(row.get("coverage_score") or 0.0),
                total_claims=int(row.get("total_claims") or 0),
                empty_trajectory=not raw.strip(),
                num_retry=retry,
                enabled_tools=_enabled_tool_names(row.get("ENABLED_TOOLS") or ""),
            )
            flattened = {t.replace("-", "_") for t in record.enabled_tools}
            for call in _iter_calls(history):
                record.calls.append(call)
                if _is_empty_result(call.result):
                    record.empty_result_servers.add(call.server)
                if not _is_failure(call.result):
                    continue
                head = call.result[:600]
                for label, needle in MODEL_RULES:
                    if needle in head:
                        record.model_hits.add(f"{label}:{call.server}")
                # A rejected name that matches an enabled tool once hyphens and
                # underscores are folded together is the model mangling the
                # separator, not a missing tool. Worth its own label: one model
                # did this 154 times across 59 tasks while every other model in
                # the fleet stayed under 20.
                if "is not enabled for task" in head or "Unknown tool:" in head:
                    if call.name.replace("-", "_") in flattened:
                        record.model_hits.add(f"tool_name_mangled:{call.server}")
                category = env_category(call.result, call.server)
                if category:
                    record.env_hits.add(f"{category}:{call.server}")
                elif call.server in CREDENTIAL_FREE_SERVERS:
                    # Suppressed above: name it so it is still counted, against
                    # the model, instead of vanishing into the unlabelled pile.
                    unscoped = env_category(call.result)
                    if unscoped in CREDENTIAL_CATEGORIES:
                        record.model_hits.add(
                            f"model_authored_request:{call.server}"
                        )
            for hit in record.env_hits:
                server = hit.split(":", 1)[1]
                if not record.recovered(server):
                    record.env_blocking_hits.add(hit)
            records.append(record)
    return records


def _model_attributed(result: str) -> bool:
    head = result[:600]
    return any(needle in head for _, needle in MODEL_RULES)


def server_health(records: Iterable[TaskRecord]) -> dict[str, dict[str, int]]:
    """Per-server call and failure counts -- the basis for the validity gate.

    ``failures`` is every failure; ``env_failures`` excludes the ones a
    MODEL_RULE claimed. Only the second number may gate validity, or a model
    that writes bad paths 86 times voids the run it was being measured in.
    """
    stats: dict[str, dict[str, int]] = collections.defaultdict(
        lambda: {"calls": 0, "failures": 0, "env_failures": 0, "empty": 0}
    )
    for record in records:
        for call in record.calls:
            entry = stats[call.server]
            entry["calls"] += 1
            if _is_failure(call.result):
                entry["failures"] += 1
                if not _model_attributed(call.result):
                    entry["env_failures"] += 1
            if _is_empty_result(call.result):
                entry["empty"] += 1
    return dict(stats)


def tool_health(records: Iterable[TaskRecord]) -> dict[str, dict[str, int]]:
    """Per-tool counts, so a dead tool inside a healthy server is still visible."""
    stats: dict[str, dict[str, int]] = collections.defaultdict(
        lambda: {"calls": 0, "failures": 0, "env_failures": 0}
    )
    for record in records:
        for call in record.calls:
            entry = stats[call.name]
            entry["calls"] += 1
            if _is_failure(call.result):
                entry["failures"] += 1
                if not _model_attributed(call.result):
                    entry["env_failures"] += 1
    return dict(stats)


def detect_asymmetric_empties(records: Iterable[TaskRecord]) -> dict[str, str]:
    """Surface servers whose search-shaped calls come back empty while others work.

    This is a review prompt, not a verdict. It caught the real wikipedia
    breakage (search 137/180 empty while get_article was 43/43 fine) and also
    fires on notion, where an empty result simply means the query matched no
    page title. Callers must not fold the output into the score.
    """
    by_tool: dict[str, dict[str, int]] = collections.defaultdict(
        lambda: {"calls": 0, "empty": 0}
    )
    for record in records:
        for call in record.calls:
            entry = by_tool[call.name]
            entry["calls"] += 1
            if _is_empty_result(call.result):
                entry["empty"] += 1

    verdicts: dict[str, str] = {}
    grouped: dict[str, list[str]] = collections.defaultdict(list)
    for tool in by_tool:
        grouped[tool.split("_")[0]].append(tool)

    for server, tools in grouped.items():
        broken = []
        healthy = []
        for tool in tools:
            entry = by_tool[tool]
            if entry["calls"] < 5:
                continue
            ratio = entry["empty"] / entry["calls"]
            if ratio >= 0.5:
                broken.append(f"{tool} {entry['empty']}/{entry['calls']}")
            elif entry["empty"] == 0:
                healthy.append(f"{tool} 0/{entry['calls']}")
        if broken and healthy:
            verdicts[server] = (
                f"broken: {', '.join(broken)}; healthy: {', '.join(healthy)}"
            )
    return verdicts


def locate_run_windows(lines: list[str], label: str) -> tuple[list[tuple[int, int]], str]:
    """Return *every* [start, end) line range that produced ``label``.

    ``mcp_eval.log`` is appended across runs, so scanning the whole file
    attributes an earlier run's failures to this one. That is not hypothetical:
    a run whose mongo fixture image was missing left 1579 `No such image`
    retries in the log, and the *next*, healthy run inherited all of them.

    But taking only the *last* save marker is just as wrong: one run was killed
    and resumed, so it wrote two chunks (105 tasks, then the remaining 415), and
    all 48 `docker exec timed out` failures lived in the chunk that got skipped.
    A resumed run and a re-run of the same label are indistinguishable from the
    log alone, so every chunk is scanned and the caller sees how many there were.

    Each end anchor is a ``Results saved to: ...MCP-Atlas-<label>.csv``. Each
    start anchor is the nearest preceding ``Loading data from``, stopping at any
    earlier ``Results saved to`` so a neighbouring run cannot be absorbed.
    """
    saved = re.compile(r"Results saved to:.*MCP-Atlas-" + re.escape(label) + r"\.csv")
    ends = [index for index, line in enumerate(lines) if saved.search(line)]
    if not ends:
        return [(0, len(lines))], "whole file (no run marker for this model)"

    windows: list[tuple[int, int]] = []
    for end in ends:
        start = 0
        for index in range(end - 1, -1, -1):
            if "Results saved to:" in lines[index]:
                start = index + 1
                break
            if "Loading data from" in lines[index]:
                start = index
                break
        windows.append((start, end + 1))

    described = ", ".join(f"lines {s + 1}-{e}" for s, e in windows)
    if len(windows) > 1:
        described += f" ({len(windows)} chunks -- resumed or re-run)"
    return windows, described


def locate_orphan_windows(
    lines: list[str], claimed: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Blocks that started a run but never wrote a save marker.

    A killed run leaves one of these, and the tasks it managed to finish are
    carried into the next attempt's CSV -- so its failures are part of that
    result even though no marker ties them to the label. They cannot be
    attributed from markers alone, so they are reported separately rather than
    merged: silently absorbing a neighbour's failures is the bug this file's
    window slicing exists to prevent.
    """
    if not claimed:
        return []
    starts = [i for i, line in enumerate(lines) if "Loading data from" in line]
    saves = [i for i, line in enumerate(lines) if "Results saved to:" in line]
    first_claimed = min(start for start, _ in claimed)
    orphans: list[tuple[int, int]] = []
    for index, start in enumerate(starts):
        stop = starts[index + 1] if index + 1 < len(starts) else len(lines)
        if any(start <= save < stop for save in saves):
            continue
        if any(s <= start < e for s, e in claimed):
            continue
        # Only a block that runs straight into this label's first chunk can have
        # fed it. Every run uses the same 500 task ids, so task overlap proves
        # nothing -- adjacency is the only usable evidence, and it correctly
        # attributes a killed block to the run that resumed it rather than to
        # every later run that happens to share the task set.
        if stop > first_claimed or any(stop <= save < first_claimed for save in saves):
            continue
        orphans.append((start, stop))
    return orphans


def scan_eval_log(
    path: Path,
    task_ids: set[str],
    label: str,
    whole_file: bool = False,
) -> dict[str, Any]:
    """Attribute infrastructure failures using this run's slice of the log."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if whole_file:
        windows, window = [(0, len(lines))], "whole file (--log-whole-file)"
    else:
        windows, window = locate_run_windows(lines, label)

    causes: collections.Counter[str] = collections.Counter()
    tasks_hit: dict[str, set[str]] = collections.defaultdict(set)
    samples: dict[str, str] = {}
    pattern = re.compile(r"for task (\S+?):\s*(.*)")
    for start, end in windows:
        for line in lines[start:end]:
            if "error on attempt" not in line:
                continue
            match = pattern.search(line)
            if not match:
                continue
            task_id, detail = match.group(1), match.group(2)
            if task_ids and task_id not in task_ids:
                continue
            for cause, needle in INFRA_LOG_RULES:
                if needle in detail:
                    break
            else:
                cause = "other"
            causes[cause] += 1
            tasks_hit[cause].add(task_id)
            samples.setdefault(cause, detail[:200])
    orphan_causes: collections.Counter[str] = collections.Counter()
    orphan_tasks: set[str] = set()
    if not whole_file:
        for start, end in locate_orphan_windows(lines, windows):
            for line in lines[start:end]:
                if "error on attempt" not in line:
                    continue
                match = pattern.search(line)
                if not match:
                    continue
                task_id, detail = match.group(1), match.group(2)
                if task_ids and task_id not in task_ids:
                    continue
                for cause, needle in INFRA_LOG_RULES:
                    if needle in detail:
                        break
                else:
                    cause = "other"
                orphan_causes[cause] += 1
                orphan_tasks.add(task_id)

    return {
        "window": window,
        "attempts": dict(causes),
        "tasks": {cause: sorted(ids) for cause, ids in tasks_hit.items()},
        "samples": samples,
        "orphan_attempts": dict(orphan_causes),
        "orphan_tasks": sorted(orphan_tasks),
    }


def _ts_key(stamp: str) -> str:
    """Second-precision key so ``...Z`` and ``...+00:00`` stamps compare cleanly."""
    return str(stamp)[:19]


def run_time_bounds(
    path: Path, label: str, whole_file: bool = False
) -> tuple[str, str] | None:
    """UTC [first, last] wall-clock of this run, read off the driver log.

    Needed because the other two logs cannot be windowed by task id: every run
    replays the same 500 tasks, so filtering by them merges *all* runs -- one
    join spanned five days and reported 215 upstream 401s that belonged to other
    runs. The driver log stamps local time and is written by the same host as the
    runtime and usage logs, so converting through the local zone is exact.
    """
    if whole_file:
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    windows, _ = locate_run_windows(lines, label)
    stamp_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    stamps: list[dt.datetime] = []
    for start, end in windows:
        for line in (lines[start:end]):
            match = stamp_re.match(line)
            if match:
                stamps.append(
                    dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                )
    if not stamps:
        return None
    # A tool call can outlive the last log line by its timeout, so pad the tail.
    first = min(stamps).astimezone(dt.timezone.utc)
    last = (max(stamps) + dt.timedelta(minutes=5)).astimezone(dt.timezone.utc)
    return first.isoformat()[:19], last.isoformat()[:19]


def scan_runtime_log(
    directory: Path, task_ids: set[str], window: tuple[str, str] | None = None
) -> dict[str, Any] | None:
    """Per-call durations and gate queue waits from ``runtime_logs/tools_*.jsonl``.

    The CSV records what a tool answered, never how long it took or how long the
    call queued. Both matter: a 150s tool timeout behind a concurrency-1 pacing
    gate produced a 299s queue wait, so one stalled upstream starved every other
    task that needed the same server. That is an environment fault the
    trajectories cannot show.

    The window is derived from the events of this run's own task ids. Two runs of
    the same task set on the same day would merge; pass a narrower directory or
    accept the union.
    """
    files = sorted(directory.glob("**/tools_*.jsonl"))
    if not files:
        return None

    started: dict[str, dict[str, Any]] = {}
    per_server: dict[str, dict[str, Any]] = collections.defaultdict(
        lambda: {"calls": 0, "transport_failures": 0, "slow_calls": 0,
                 "max_seconds": 0.0, "max_queue_seconds": 0.0}
    )
    slow: list[dict[str, Any]] = []
    stamps: list[str] = []
    for file in files:
        with file.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"tool_call_' not in line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if task_ids and event.get("task_id") not in task_ids:
                    continue
                kind = event.get("event")
                if kind == "tool_call_started":
                    if window and not (
                        window[0] <= _ts_key(event.get("timestamp", "")) <= window[1]
                    ):
                        continue
                    started[event["call_id"]] = event
                    continue
                if kind not in ("tool_call_completed", "tool_call_failed"):
                    continue
                begin = started.pop(event.get("call_id", ""), None)
                if begin is None:
                    continue
                stamps.append(begin["timestamp"])
                server = str(begin.get("tool_name", "")).split("_")[0]
                seconds = float(event.get("duration_seconds") or 0.0)
                queued = float(begin.get("rate_limit_queued_ms") or 0) / 1000.0
                entry = per_server[server]
                entry["calls"] += 1
                entry["max_seconds"] = max(entry["max_seconds"], seconds)
                entry["max_queue_seconds"] = max(entry["max_queue_seconds"], queued)
                if kind == "tool_call_failed":
                    entry["transport_failures"] += 1
                if seconds >= 100:
                    entry["slow_calls"] += 1
                    slow.append({
                        "at": begin["timestamp"],
                        "tool": begin.get("tool_name"),
                        "seconds": round(seconds, 1),
                        "queued_seconds": round(queued, 1),
                        "task": begin.get("task_id"),
                    })
    if not stamps:
        return None
    return {
        "window": [min(stamps), max(stamps)],
        "servers": {
            server: {k: (round(v, 1) if isinstance(v, float) else v)
                     for k, v in entry.items()}
            for server, entry in sorted(per_server.items())
        },
        "slow_calls": sorted(slow, key=lambda item: item["at"])[:40],
    }


def scan_usage_log(
    directory: Path, window: tuple[str, str] | None
) -> dict[str, Any] | None:
    """Upstream HTTP status codes per credentialed service, inside ``window``.

    This is the only place the truth about a masked error lives. ``Error: 'job'``
    resolved to four upstream 400s once these were joined, and a 20-minute
    window in which one shared key answered 35 requests with HTTP 401 across
    oxylabs, exa and brave was invisible in every other artifact.
    """
    files = sorted(directory.glob("**/*.jsonl"))
    if not files:
        return None
    if window is None:
        # Without a window this would sum every run ever logged, which is exactly
        # the cross-run contamination this file exists to avoid.
        return {"window": None, "statuses": {}, "non_200": [],
                "note": "no run window available (pass --eval-log); not scanned"}
    start, end = window

    per_service: dict[str, collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    failures: list[dict[str, Any]] = []
    for file in files:
        with file.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                stamp = _ts_key(record.get("ts") or "")
                if not (start <= stamp <= end):
                    continue
                service = str(record.get("service") or file.stem.split("_")[0])
                status = str(record.get("status"))
                per_service[service][status] += 1
                if record.get("status") != 200:
                    failures.append({
                        "at": stamp,
                        "service": service,
                        "status": record.get("status"),
                        "error": record.get("error"),
                        "duration_ms": record.get("duration_ms"),
                    })
    if not per_service:
        return None
    return {
        "window": [start, end],
        "statuses": {s: dict(c) for s, c in sorted(per_service.items())},
        "non_200": sorted(failures, key=lambda item: item["at"])[:60],
    }


def build_report(
    label: str,
    records: list[TaskRecord],
    eval_log: dict[str, Any] | None,
    runtime_log: dict[str, Any] | None = None,
    usage_log: dict[str, Any] | None = None,
) -> dict[str, Any]:
    total = len(records)
    passed = sum(1 for r in records if r.passed)
    infra = [r for r in records if r.empty_trajectory]
    failed = [r for r in records if not r.passed]

    def attributable(hits: set[str]) -> set[str]:
        return {h for h in hits if h.split(":", 1)[0] not in UNATTRIBUTABLE}

    env_failed = [r for r in failed if attributable(r.env_hits) and not r.empty_trajectory]
    env_blocked = [
        r for r in failed
        if attributable(r.env_blocking_hits) and not r.empty_trajectory
    ]
    model_only = [r for r in failed if not r.env_hits and not r.empty_trajectory]

    # Reported for review only -- deliberately not merged into env_hits, so an
    # ambiguous empty result can never inflate the ceiling or void the run.
    asymmetric = detect_asymmetric_empties(records)

    health = server_health(records)
    tools = tool_health(records)
    known_servers = set().union(*(r.enabled_servers for r in records)) if records else set()

    # Only failures no MODEL_RULE claimed, and only for servers that were really
    # offered to some task. Both guards exist because a single run was voided by
    # "weather-data-weather 27/27" (a mangled tool name, not a server) and
    # "desktop-commander 86/146" (58 no-match searches plus 28 path denials).
    broken_servers = {
        server: stats
        for server, stats in health.items()
        if server in known_servers
        and stats["calls"] >= 20
        and stats["env_failures"] / stats["calls"] >= 0.5
    }
    broken_tools = {
        tool: stats
        for tool, stats in tools.items()
        if stats["calls"] >= 8 and stats["env_failures"] == stats["calls"]
    }

    # Upper bound only: assume every *blocked* failure would have passed. The
    # touched-based ceiling is kept beside it because that is what older reports
    # printed, and it runs 2-3x higher.
    ceiling = (passed + len(env_blocked) + len(infra)) / total if total else 0.0
    ceiling_touched = (passed + len(env_failed) + len(infra)) / total if total else 0.0

    env_counter: collections.Counter[str] = collections.Counter()
    blocking_counter: collections.Counter[str] = collections.Counter()
    model_counter: collections.Counter[str] = collections.Counter()
    masked_counter: collections.Counter[str] = collections.Counter()
    for record in records:
        for hit in record.env_hits:
            if hit.split(":", 1)[0] in UNATTRIBUTABLE:
                masked_counter[hit] += 1
            else:
                env_counter[hit] += 1
        for hit in record.model_hits:
            model_counter[hit] += 1
    # Scoped to failing tasks so this table sums to env_blocking_failures. A
    # blocking fault in a task that passed anyway is real but costs nothing.
    for record in env_blocked:
        for hit in record.env_blocking_hits:
            if hit.split(":", 1)[0] not in UNATTRIBUTABLE:
                blocking_counter[hit] += 1

    blockers: list[str] = []
    if infra:
        blockers.append(
            f"{len(infra)} tasks produced no trajectory at all "
            f"({len(infra) / total:.1%} of the run)"
        )
    if broken_servers:
        blockers.append(
            "servers failing >=50% of calls (model-caused failures excluded): "
            + ", ".join(sorted(broken_servers))
        )

    return {
        "label": label,
        "total": total,
        "passed": passed,
        "score": passed / total if total else 0.0,
        "mean_coverage": (sum(r.coverage for r in records) / total) if total else 0.0,
        "infra_failures": len(infra),
        "infra_task_ids": sorted(r.task_id for r in infra),
        "env_blocking_failures": len(env_blocked),
        "env_blocking_task_ids": sorted(r.task_id for r in env_blocked),
        "env_touched_failures": len(env_failed),
        "model_only_failures": len(model_only),
        "score_ceiling": ceiling,
        "score_ceiling_touched": ceiling_touched,
        "env_blocking_signatures": blocking_counter.most_common(),
        "env_signatures": env_counter.most_common(),
        "masked_signatures": masked_counter.most_common(),
        "model_signatures": model_counter.most_common(),
        "server_health": health,
        "broken_servers": broken_servers,
        "broken_tools": broken_tools,
        "asymmetric_empty": asymmetric,
        "eval_log": eval_log,
        "runtime_log": runtime_log,
        "usage_log": usage_log,
        "blockers": blockers,
        "verdict": "INVALID" if blockers else "OK",
    }


def corroborate(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Separate infrastructure defects from single-model behaviour."""
    seen: dict[str, set[str]] = collections.defaultdict(set)
    for report in reports:
        for server, stats in report["server_health"].items():
            if stats["calls"] >= 20 and stats["failures"] / stats["calls"] >= 0.10:
                seen[server].add(report["label"])
    runs = len(reports)
    return {
        "runs": [r["label"] for r in reports],
        "infrastructure": {
            server: sorted(labels)
            for server, labels in seen.items()
            if len(labels) == runs and runs > 1
        },
        "model_specific": {
            server: sorted(labels)
            for server, labels in seen.items()
            if len(labels) < runs
        },
    }


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# {report['label']}")
    add("")
    add(f"**Score (fixed denominator): {report['passed']}/{report['total']} = "
        f"{report['score']:.1%}**  |  mean coverage {report['mean_coverage']:.4f}")
    add("")
    add(f"- run verdict: **{report['verdict']}**")
    add(f"- score ceiling if every environment-**blocked** failure had passed: "
        f"{report['score_ceiling']:.1%} "
        f"(+{(report['score_ceiling'] - report['score']) * 100:.1f} pts, upper bound only)")
    add(f"- no-trajectory tasks (never reached the model): {report['infra_failures']}")
    add(f"- **failures an environment fault blocked** (server never recovered): "
        f"{report['env_blocking_failures']}")
    add(f"- failures that merely touched an environment fault (recovered later): "
        f"{report['env_touched_failures']} "
        f"-> old-style ceiling {report['score_ceiling_touched']:.1%}")
    add(f"- failures with a clean environment (model's own): {report['model_only_failures']}")
    add("")
    if report["blockers"]:
        add("## Why this run is not comparable")
        for blocker in report["blockers"]:
            add(f"- {blocker}")
        add("")
    if report["eval_log"] and report["eval_log"]["attempts"]:
        add("## Infrastructure failures (from the driver log)")
        add("")
        add(f"log window: {report['eval_log']['window']}")
        add("")
        add("| cause | retry attempts | tasks |")
        add("|---|---:|---:|")
        for cause, count in sorted(
            report["eval_log"]["attempts"].items(), key=lambda kv: -kv[1]
        ):
            add(f"| {cause} | {count} | {len(report['eval_log']['tasks'].get(cause, []))} |")
        add("")
        orphans = report["eval_log"].get("orphan_attempts") or {}
        if orphans:
            add(f"Plus {sum(orphans.values())} retry attempts across "
                f"{len(report['eval_log']['orphan_tasks'])} of this run's tasks in a "
                f"log block that never wrote a save marker (a killed run whose "
                f"finished tasks were carried into this CSV): "
                + ", ".join(f"{c} x{n}" for c, n in sorted(orphans.items(), key=lambda kv: -kv[1]))
                + ". Not attributed automatically -- confirm before counting.")
            add("")
    if report["env_blocking_signatures"]:
        add("## Environment faults that blocked a failing task")
        add("")
        add("Counted in the ceiling. The server never returned usable content")
        add("again in that task, so the model had no way past it.")
        add("")
        add("| signature | tasks |")
        add("|---|---:|")
        for signature, count in report["env_blocking_signatures"][:25]:
            add(f"| {signature} | {count} |")
        add("")
    if report["env_signatures"]:
        add("## Environment faults seen at all (not necessarily blocking)")
        add("")
        add("| signature | tasks |")
        add("|---|---:|")
        for signature, count in report["env_signatures"][:25]:
            add(f"| {signature} | {count} |")
        add("")
    if report["masked_signatures"]:
        add("## Unattributable: the error text destroyed its own cause")
        add("")
        add("Excluded from the ceiling either way. Join `--usage-log` to recover")
        add("the upstream status these hid.")
        add("")
        add("| signature | tasks |")
        add("|---|---:|")
        for signature, count in report["masked_signatures"][:25]:
            add(f"| {signature} | {count} |")
        add("")
    if report["broken_tools"]:
        add("## Tool-level outages (server looked healthy, this tool did not)")
        add("")
        add("| tool | failures/calls |")
        add("|---|---:|")
        for tool, stats in sorted(report["broken_tools"].items()):
            add(f"| {tool} | {stats['env_failures']}/{stats['calls']} |")
        add("")
    if report["model_signatures"]:
        add("## Model faults (counted against the model)")
        add("")
        add("| signature | tasks |")
        add("|---|---:|")
        for signature, count in report["model_signatures"][:25]:
            add(f"| {signature} | {count} |")
        add("")
    if report["asymmetric_empty"]:
        add("## Needs a human look: empty on some tools, healthy on others")
        add("")
        add("Not scored either way — an empty result cannot be told apart from a")
        add("query that genuinely matched nothing. Check a query you know should hit.")
        add("")
        for server, detail in sorted(report["asymmetric_empty"].items()):
            add(f"- **{server}** — {detail}")
        add("")
    if report.get("usage_log"):
        add("## Upstream HTTP status (credentialed services)")
        add("")
        add(f"window: {report['usage_log']['window'][0]} .. {report['usage_log']['window'][1]}")
        add("")
        add("| service | statuses |")
        add("|---|---|")
        for service, statuses in report["usage_log"]["statuses"].items():
            rendered = ", ".join(f"{k}x{v}" for k, v in sorted(statuses.items()))
            add(f"| {service} | {rendered} |")
        add("")
        if report["usage_log"]["non_200"]:
            add("Non-200 responses (the cause a masked tool error hid):")
            add("")
            for item in report["usage_log"]["non_200"][:20]:
                add(f"- `{item['at']}` {item['service']} -> "
                    f"{item['status']}{' ' + str(item['error']) if item['error'] else ''}")
            add("")
    if report.get("runtime_log"):
        slow = report["runtime_log"]["slow_calls"]
        worst = max(
            (s["max_queue_seconds"] for s in report["runtime_log"]["servers"].values()),
            default=0.0,
        )
        add("## Call timing (from the runtime log)")
        add("")
        add(f"window: {report['runtime_log']['window'][0]} .. "
            f"{report['runtime_log']['window'][1]}")
        add(f"worst pacing-gate queue wait: {worst:.0f}s")
        add("")
        add("| server | calls | transport fails | >=100s | max s | max queue s |")
        add("|---|---:|---:|---:|---:|---:|")
        for server, stats in sorted(
            report["runtime_log"]["servers"].items(),
            key=lambda kv: (-kv[1]["slow_calls"], -kv[1]["max_queue_seconds"]),
        ):
            if not (stats["transport_failures"] or stats["slow_calls"]
                    or stats["max_queue_seconds"] >= 10):
                continue
            add(f"| {server} | {stats['calls']} | {stats['transport_failures']} | "
                f"{stats['slow_calls']} | {stats['max_seconds']:.0f} | "
                f"{stats['max_queue_seconds']:.0f} |")
        add("")
        if slow:
            add(f"slowest calls: " + ", ".join(
                f"{item['tool']} {item['seconds']:.0f}s (queued {item['queued_seconds']:.0f}s)"
                for item in sorted(slow, key=lambda i: -i["seconds"])[:5]))
            add("")
    add("## Server health")
    add("")
    add("`env` excludes failures a MODEL_RULE claimed -- only that column gates validity.")
    add("")
    add("| server | failures/calls | rate | env failures | env rate |")
    add("|---|---:|---:|---:|---:|")
    ranked = sorted(
        report["server_health"].items(),
        key=lambda kv: -(kv[1]["failures"] / kv[1]["calls"]) if kv[1]["calls"] else 0,
    )
    for server, stats in ranked:
        if stats["calls"] < 20 or not stats["failures"]:
            continue
        rate = stats["failures"] / stats["calls"]
        env_rate = stats["env_failures"] / stats["calls"]
        add(f"| {server} | {stats['failures']}/{stats['calls']} | {rate:.1%} "
            f"| {stats['env_failures']} | {env_rate:.1%} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scored", action="append", required=True, type=Path,
                        help="scored_<model>.csv; repeat for cross-run corroboration")
    parser.add_argument("--eval-log", type=Path,
                        help="completion_results/mcp_eval.log, for infra attribution")
    parser.add_argument("--runtime-log", type=Path,
                        help="completion_results/runtime_logs, for call durations "
                             "and pacing-gate queue waits")
    parser.add_argument("--usage-log", type=Path,
                        help="mcp_usage_log, for upstream HTTP status codes -- the "
                             "only way to attribute a masked tool error")
    parser.add_argument("--log-whole-file", action="store_true",
                        help="disable run-window slicing (the log is appended across "
                             "runs, so this will mix in earlier runs' failures)")
    parser.add_argument("--out-dir", type=Path, default=Path("verify_v2"))
    parser.add_argument("--corroborate", action="store_true",
                        help="separate infrastructure defects from model behaviour")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []

    for path in args.scored:
        label = path.stem.replace("scored_", "")
        records = load_run(path)
        log_data = None
        if args.eval_log and args.eval_log.exists():
            log_data = scan_eval_log(
                args.eval_log,
                {r.task_id for r in records},
                label,
                whole_file=args.log_whole_file,
            )
        task_ids = {r.task_id for r in records}
        bounds = None
        if args.eval_log and args.eval_log.exists():
            bounds = run_time_bounds(args.eval_log, label, args.log_whole_file)
        runtime_data = None
        if args.runtime_log and args.runtime_log.exists():
            runtime_data = scan_runtime_log(args.runtime_log, task_ids, bounds)
        usage_data = None
        if args.usage_log and args.usage_log.exists():
            usage_data = scan_usage_log(args.usage_log, bounds)

        report = build_report(label, records, log_data, runtime_data, usage_data)
        reports.append(report)

        markdown = render(report)
        (args.out_dir / f"{label}.md").write_text(markdown, encoding="utf-8")
        (args.out_dir / f"{label}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(markdown)
        print()

    if args.corroborate and len(reports) > 1:
        cross = corroborate(reports)
        (args.out_dir / "corroboration.json").write_text(
            json.dumps(cross, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("# Cross-run corroboration")
        print()
        print(f"runs compared: {', '.join(cross['runs'])}")
        print()
        print("Infrastructure defects (failing in every run):")
        for server in sorted(cross["infrastructure"]):
            print(f"  - {server}")
        print()
        print("Model-specific (failing in only some runs):")
        for server, labels in sorted(cross["model_specific"].items()):
            print(f"  - {server}: {', '.join(labels)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
