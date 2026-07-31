import json
import re
from pathlib import Path

from compact import _validate_summary_sections


FIXTURES_PATH = Path(__file__).parent / "fixtures" / "compact_summary_cases.json"
_EXACT_VALUE_RE = re.compile(
    r"(?:/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+|"
    r"\b[0-9a-f]{7,40}\b|"
    r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?\b|"
    r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|"
    r"\b\d+(?:\.\d+)?(?:%|M)?\b)"
)


def load_cases(path: Path = FIXTURES_PATH) -> list[dict]:
    return json.loads(path.read_text())


def render_case(case: dict, secrets: dict[str, str]) -> dict:
    rendered = json.loads(json.dumps(case))
    for name, value in secrets.items():
        placeholder = "{{" + name + "}}"
        rendered["context"] = rendered["context"].replace(placeholder, value)
        rendered["recent_messages"] = [
            message.replace(placeholder, value)
            for message in rendered["recent_messages"]
        ]
    return rendered


def _exact_values_are_sourced(case: dict, summary: str) -> bool:
    source = case["context"] + "\n" + "\n".join(case["recent_messages"])
    allowed = set(case.get("allowed_exact_values", []))
    audited = re.sub(r"(?m)^(\s*)\d+[.)](?=\s)", r"\1", summary)
    for value in _EXACT_VALUE_RE.findall(audited):
        if value not in source and value not in allowed:
            return False
    return True


def score_case(
    case: dict,
    summary: str,
    files: dict[str, str],
    raw_secrets: list[str] | None = None,
) -> dict:
    raw_secrets = raw_secrets or []
    normalized = summary.replace("\r\n", "\n")
    lower = normalized.casefold()
    categories = {
        "sections": _validate_summary_sections(normalized),
        "anchors": all(anchor in normalized for anchor in case["required_anchors"]),
        "forbidden_claims": all(
            forbidden.casefold() not in lower
            for forbidden in case.get("forbidden_claims", [])
        ),
        "recent_verbatim": all(
            message.replace("\r\n", "\n") in normalized
            for message in case["recent_messages"]
        ),
        "secrets": all(secret not in normalized for secret in raw_secrets),
        "file_state": files == case.get("expected_files", {}),
        "source_ledger": _exact_values_are_sourced(case, normalized),
        "no_transcript_dump": len(normalized) <= len(case["context"]) * 3 + 4000,
    }
    failed = [name for name, passed in categories.items() if not passed]
    return {
        "passed": not failed,
        "categories": categories,
        "failed_categories": failed,
    }
