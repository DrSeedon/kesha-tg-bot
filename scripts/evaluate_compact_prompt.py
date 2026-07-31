#!/usr/bin/env python3
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import random
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from claude_session import (
    ClaudeSession,
    EXPECTED_CONTEXT_TOKENS,
    NORMAL_TURN_RESERVE_TOKENS,
)
from compact import (
    COMPACT_PROMPT,
    CONTINUATION_PREAMBLE,
    _collect_summary,
    compact_session,
)
from compact_summary_scorer import load_cases, render_case, score_case


MODEL = "claude-opus-5"
EVALUATION_SEED = "task-14-compact-v2"
DEFAULT_OUTPUT = ROOT / "docs" / "tasks" / "14" / "compact-eval-v2.json"
GENERATION_TIMEOUT_SECONDS = 600


class EvaluationSession(ClaudeSession):
    def __init__(self, *args, evaluation_tools: list[str], **kwargs):
        self._evaluation_tools = evaluation_tools
        self.sent_prompts = []
        super().__init__(*args, **kwargs)

    def _make_options(self):
        options = super()._make_options()
        options.tools = self._evaluation_tools
        options.max_turns = 8
        return options

    async def send_message(self, text):
        self.sent_prompts.append(text)
        async for chunk in super().send_message(text):
            yield chunk


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def is_transient_overload(value: str) -> bool:
    return "529" in value or "overload" in value.casefold()


def file_state(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == ".session":
            continue
        files[path.relative_to(root).as_posix()] = path.read_text(errors="replace")
    return files


def file_state_hash(files: dict[str, str]) -> str:
    return digest(json.dumps(files, ensure_ascii=False, sort_keys=True))


def seed_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def cell_seed(case_id: str, run: int) -> str:
    return digest(f"{EVALUATION_SEED}:{case_id}:{run}")


def runtime_secrets(case: dict, run: int) -> dict[str, str]:
    values = {}
    for name in case.get("secret_placeholders", []):
        synthetic = digest(f"{cell_seed(case['id'], run)}:{name}")[:24]
        if name == "TOKEN":
            values[name] = f"sk-ant-{synthetic}"
        else:
            values[name] = f"synthetic-password-{synthetic}"
    return values


def system_prompt(case: dict) -> str:
    known_files = sorted(case.get("initial_files", {}))
    file_contract = (
        "The isolated working directory contains exactly these canonical files: "
        + (", ".join(known_files) if known_files else "none")
        + ". Do not search for or invent any other note. If this list is empty, "
        "there is no correct pre-save destination and you must not call file tools."
    )
    return (
        "This is an isolated compact-prompt evaluation. Treat the following as "
        "the complete authoritative conversation and tool evidence. Do not invent "
        "facts outside it. Do not mention or infer facts/preferences from ambient "
        "system instructions, response-style rules, or the runtime date. The "
        "synthetic paths and credentials are not real. "
        f"{file_contract}\n\n"
        + case["context"]
    )


async def disconnect(session: ClaudeSession) -> None:
    try:
        await session._safe_disconnect()
    except Exception:
        pass


async def normal_generation(case: dict, root: Path) -> tuple[str, str | None]:
    session = EvaluationSession(
        cwd=str(root),
        model=MODEL,
        system_prompt=system_prompt(case),
        session_file=root / ".session",
        evaluation_tools=["Read", "Edit", "Write"]
        if case.get("initial_files")
        else [],
    )
    try:
        return await _collect_summary(session)
    finally:
        await disconnect(session)


async def reserve_recovery_generation(
    case: dict,
    root: Path,
) -> tuple[str, str | None, dict[str, bool | str]]:
    evidence: dict[str, bool | str] = {
        "reserve_rejected_without_query": False,
        "manual_floor_admitted": False,
        "candidate_sid_changed": False,
        "candidate_resumed": False,
    }
    session = EvaluationSession(
        cwd=str(root),
        model=MODEL,
        system_prompt=system_prompt(case),
        session_file=root / ".session",
        evaluation_tools=[],
    )
    try:
        source_ready = []
        async for chunk in session.send_message(
            "Reply with exactly SOURCE_READY and nothing else."
        ):
            if chunk.get("type") in {"text", "text_delta"}:
                source_ready.append(str(chunk.get("content") or ""))
            elif chunk.get("type") == "error":
                content = str(chunk.get("content") or "")
                return "", (
                    "transient_overloaded"
                    if is_transient_overload(content)
                    else "source_session_error"
                ), evidence
        if "SOURCE_READY" not in "".join(source_ready) or not session.session_id:
            return "", "source_session_missing", evidence

        client = session._client
        real_get_usage = client.get_context_usage
        real_query = client.query
        real_usage = await real_get_usage()
        if not isinstance(real_usage, dict):
            return "", "missing_authoritative_usage", evidence

        admitted_prompt = "reserve recovery candidate"
        required = NORMAL_TURN_RESERVE_TOKENS + len(
            admitted_prompt.encode("utf-8")
        )
        synthetic_usage = dict(real_usage)
        synthetic_usage["totalTokens"] = (
            EXPECTED_CONTEXT_TOKENS - required + 1
        )
        query_calls = 0

        async def counted_query(text):
            nonlocal query_calls
            query_calls += 1
            return await real_query(text)

        async def reserved_usage():
            return synthetic_usage

        client.query = counted_query
        client.get_context_usage = reserved_usage
        try:
            reserve = await session.check_context_reserve(admitted_prompt)
        finally:
            client.query = real_query
            client.get_context_usage = real_get_usage
        if reserve.get("ok") or reserve.get("reason") != "reserve":
            return "", "reserve_rejection_missing", evidence
        if query_calls:
            return "", "reserve_rejection_queried", evidence
        evidence["reserve_rejected_without_query"] = True

        manual = await session.check_context_reserve(manual=True)
        if not manual.get("ok"):
            return (
                "",
                f"manual_floor_{manual.get('reason') or 'failed'}",
                evidence,
            )
        evidence["manual_floor_admitted"] = True

        source_sid = session.session_id
        evidence["source_sid_sha256"] = digest(source_sid)
        result = await compact_session(session)
        if not result.get("ok"):
            return "", str(result.get("reason") or "recovery_failed"), evidence
        preamble = next(
            (
                text
                for text in session.sent_prompts
                if text.startswith("[PREVIOUS CONTEXT SUMMARY")
            ),
            "",
        )
        if not preamble:
            return "", "missing_recovery_preamble", evidence
        if not session.session_id or session.session_id == source_sid:
            return "", "candidate_sid_not_committed", evidence
        evidence["candidate_sid_changed"] = True
        evidence["candidate_sid_sha256"] = digest(session.session_id)

        control = []
        async for chunk in session.send_message(
            "Reply with exactly RESUME_OK and nothing else."
        ):
            if chunk.get("type") in {"text", "text_delta"}:
                control.append(str(chunk.get("content") or ""))
            elif chunk.get("type") == "error":
                content = str(chunk.get("content") or "")
                return "", (
                    "transient_overloaded"
                    if is_transient_overload(content)
                    else "candidate_resume_error"
                ), evidence
        if "RESUME_OK" not in "".join(control):
            return "", "candidate_resume_missing", evidence
        evidence["candidate_resumed"] = True

        prefix, suffix = CONTINUATION_PREAMBLE.split("{summary}")
        return preamble[len(prefix): -len(suffix)], None, evidence
    finally:
        await disconnect(session)


async def evaluate_one(case: dict, run: int) -> tuple[dict, list[str]]:
    secret_values = runtime_secrets(case, run)
    rendered = render_case(case, secret_values)
    with tempfile.TemporaryDirectory(prefix=f"compact-{case['id']}-{run}-") as raw:
        root = Path(raw)
        seed_files(root, rendered.get("initial_files", {}))
        try:
            recovery_evidence = {}
            if rendered.get("requires_reserve_recovery"):
                summary, reason, recovery_evidence = await asyncio.wait_for(
                    reserve_recovery_generation(rendered, root),
                    timeout=GENERATION_TIMEOUT_SECONDS,
                )
            else:
                summary, reason = await asyncio.wait_for(
                    normal_generation(rendered, root),
                    timeout=GENERATION_TIMEOUT_SECONDS,
                )
            files = file_state(root)
            score = score_case(
                rendered,
                summary,
                files,
                list(secret_values.values()),
            )
            passed = reason is None and score["passed"]
            entry = {
                "case": rendered["id"],
                "run": run,
                "seed_sha256": cell_seed(rendered["id"], run),
                "passed": passed,
                "generation_completed": reason in {
                    None,
                    "empty_summary",
                    "invalid_summary",
                },
                "reason": reason or (
                    None if passed else ",".join(score["failed_categories"])
                ),
                "summary_sha256": digest(summary) if summary else None,
                "summary_chars": len(summary),
                "file_state_sha256": file_state_hash(files),
                "categories": score["categories"],
                "reserve_recovery": bool(
                    rendered.get("requires_reserve_recovery")
                ),
                "recovery_evidence": recovery_evidence,
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            entry = {
                "case": rendered["id"],
                "run": run,
                "seed_sha256": cell_seed(rendered["id"], run),
                "passed": False,
                "generation_completed": False,
                "reason": type(exc).__name__,
                "summary_sha256": None,
                "summary_chars": 0,
                "file_state_sha256": file_state_hash(file_state(root)),
                "categories": {},
                "reserve_recovery": bool(
                    rendered.get("requires_reserve_recovery")
                ),
                "recovery_evidence": {},
            }
        return entry, list(secret_values.values())


def entry_key(entry: dict) -> tuple[str, int]:
    return entry["case"], int(entry["run"])


def load_evidence(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("compact evaluation checkpoint is corrupt") from exc
    if payload.get("schema") != 2 or not isinstance(payload.get("entries"), list):
        raise RuntimeError("compact evaluation checkpoint has an unsupported schema")
    entries = payload["entries"]
    keys = [entry_key(entry) for entry in entries]
    if len(keys) != len(set(keys)):
        raise RuntimeError("compact evaluation checkpoint contains duplicate cells")
    return entries


def merge_entry(entries: list[dict], candidate: dict) -> list[dict]:
    key = entry_key(candidate)
    merged = []
    found = False
    for existing in entries:
        if entry_key(existing) != key:
            merged.append(existing)
            continue
        found = True
        if existing.get("status") in {"passed", "failed"}:
            if existing != candidate:
                raise RuntimeError(f"completed evaluation cell is immutable: {key}")
            merged.append(existing)
        else:
            merged.append(candidate)
    if not found:
        merged.append(candidate)
    return merged


def _atomic_write(path: Path, serialized: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def write_evidence(path: Path, entries: list[dict], raw_secrets: list[str]) -> None:
    passed = (
        len(entries) == 30
        and all(entry.get("status") == "passed" for entry in entries)
    )
    failed = any(entry.get("status") == "failed" for entry in entries)
    gate = "PASSED" if passed else ("FAILED" if failed else "INCOMPLETE")
    payload = {
        "schema": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "evaluation_seed_sha256": digest(EVALUATION_SEED),
        "sdk_version": importlib.metadata.version("claude-agent-sdk"),
        "cli_version": subprocess.check_output(
            ["claude", "--version"],
            text=True,
        ).strip(),
        "required_runs": 30,
        "completed_runs": sum(
            entry.get("status") in {"passed", "failed"} for entry in entries
        ),
        "passed_runs": sum(entry.get("status") == "passed" for entry in entries),
        "promotion_gate": gate,
        "entries": sorted(entries, key=lambda item: (item["case"], item["run"])),
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if any(secret in serialized for secret in raw_secrets):
        raise RuntimeError("raw synthetic secret reached evidence artifact")
    _atomic_write(path, serialized)


def classify_entry(entry: dict) -> str:
    if entry.get("passed"):
        return "passed"
    if entry.get("generation_completed"):
        return "failed"
    return "incomplete"


async def run_cell(
    case: dict,
    run: int,
    *,
    evaluator=evaluate_one,
    previous_attempts: int = 0,
    max_attempts: int = 3,
    retry_base_seconds: float = 30,
    sleep_fn=asyncio.sleep,
    checkpoint=None,
) -> tuple[dict, list[str]]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if retry_base_seconds < 0:
        raise ValueError("retry_base_seconds must not be negative")
    rng = random.Random(int(cell_seed(case["id"], run)[:16], 16))
    raw_secrets: list[str] = []
    for local_attempt in range(1, max_attempts + 1):
        entry, raw_secrets = await evaluator(case, run)
        entry["attempts"] = previous_attempts + local_attempt
        entry["status"] = classify_entry(entry)
        if checkpoint is not None:
            checkpoint(entry, raw_secrets)
        if entry["reason"] != "transient_overloaded":
            return entry, raw_secrets
        if local_attempt == max_attempts:
            return entry, raw_secrets
        exponential = retry_base_seconds * (2 ** (local_attempt - 1))
        delay = min(300.0, exponential + rng.uniform(0, exponential * 0.2))
        print(
            f"{case['id']} run={run}: transient overload, "
            f"retry {local_attempt + 1}/{max_attempts} in {delay:.1f}s",
            flush=True,
        )
        await sleep_fn(delay)
    raise AssertionError("unreachable")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--case")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-base-seconds", type=float, default=30)
    args = parser.parse_args()
    if args.runs != 3:
        raise SystemExit("promotion gate requires exactly --runs 3")

    cases = [case for case in load_cases() if not args.case or case["id"] == args.case]
    entries = load_evidence(args.output)
    all_secrets = [
        secret
        for case in load_cases()
        for run in range(1, 4)
        for secret in runtime_secrets(case, run).values()
    ]
    for case in cases:
        for run in range(1, args.runs + 1):
            key = (case["id"], run)
            existing = next(
                (entry for entry in entries if entry_key(entry) == key),
                None,
            )
            if existing and existing.get("status") in {"passed", "failed"}:
                continue

            def checkpoint(candidate, _raw_secrets):
                nonlocal entries
                entries = merge_entry(entries, candidate)
                write_evidence(args.output, entries, all_secrets)

            entry, _raw_secrets = await run_cell(
                case,
                run,
                previous_attempts=int(existing.get("attempts", 0)) if existing else 0,
                max_attempts=args.max_attempts,
                retry_base_seconds=args.retry_base_seconds,
                checkpoint=checkpoint,
            )
            print(
                f"{case['id']} run={run}: "
                f"{entry['status'].upper()} {entry['reason'] or ''}",
                flush=True,
            )

    write_evidence(args.output, entries, all_secrets)
    return 0 if (
        len(entries) == 30
        and all(item.get("status") == "passed" for item in entries)
    ) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
