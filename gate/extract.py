"""Output parsing and severity enforcement.

Ported from extract-stage.js: JSON extraction from Claude transcripts,
exploit scenario enforcement, and fix stage normalization.
"""

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_json_from_text(text: str) -> dict | None:
    """Extract JSON from text that may contain markdown fences, prose, etc.

    Tries three strategies in order:
    1. Extract from ```json ... ``` fences
    2. Full text as JSON
    3. Find outermost { ... } braces

    Ported from extractJsonFromText() in shared/utils.js.
    """
    if not text or not text.strip():
        return None

    # Strategy 1: JSON fence
    fence_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n\s*```", text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Strategy 2: full text parse
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Strategy 3: outermost braces
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        try:
            return json.loads(text[brace_start : brace_end + 1])
        except json.JSONDecodeError:
            return None

    return None


def extract_from_transcript(raw_content: str) -> dict | None:
    """Parse a Claude raw transcript and extract structured result.

    Handles both JSON transcript arrays (--output-format json) and plain text.
    Ported from extractFromTranscript() in extract-stage.js.
    """
    transcript = None
    try:
        transcript = json.loads(raw_content)
    except json.JSONDecodeError:
        return extract_json_from_text(raw_content)

    # Walk assistant messages last-to-first looking for JSON
    if isinstance(transcript, list):
        for i in range(len(transcript) - 1, -1, -1):
            msg = transcript[i]
            if msg.get("role") != "assistant":
                continue

            content = msg.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    b.get("text", "") for b in content if b.get("type") == "text"
                )
            else:
                text = ""

            parsed = extract_json_from_text(text)
            if parsed and (
                parsed.get("findings") is not None
                or parsed.get("pass") is not None
                or parsed.get("fixed") is not None
            ):
                return parsed

    # Try transcript.result
    if isinstance(transcript, dict) and transcript.get("result"):
        result = transcript["result"]
        text = result if isinstance(result, str) else json.dumps(result)
        parsed = extract_json_from_text(text)
        if parsed:
            return parsed

    return extract_json_from_text(raw_content)


def extract_stage_output(raw_path: Path, stage: str) -> dict | None:
    """Read a raw transcript file and extract the structured stage result.

    Also applies stage-specific post-processing:
    - Fix stages: normalize fixed/not_fixed arrays
    - Security: enforce exploit scenarios
    - All: ensure findings array and pass flag exist
    """
    try:
        raw_content = raw_path.read_text()
    except (OSError, FileNotFoundError):
        logger.warning(f"Could not read raw transcript: {raw_path}")
        return None

    if not raw_content.strip():
        return None

    parsed = extract_from_transcript(raw_content)
    if not parsed:
        return None

    # Fix stage normalization
    if stage == "fix":
        if not isinstance(parsed.get("fixed"), list):
            parsed["fixed"] = []
        if not isinstance(parsed.get("not_fixed"), list):
            parsed["not_fixed"] = []
        if not parsed.get("stats"):
            parsed["stats"] = {"total_findings": 0, "fixed": 0, "not_fixed": 0}
        parsed["pass"] = True
        return parsed

    # Ensure findings array
    if not isinstance(parsed.get("findings"), list):
        parsed["findings"] = []

    # Derive pass flag if missing
    if parsed.get("pass") is None:
        parsed["pass"] = all(
            f.get("severity") not in ("error", "critical") for f in parsed["findings"]
        )

    # Validate findings structure
    parsed = validate_stage_output(parsed, stage)

    # Security-specific: enforce exploit scenarios
    if stage == "security":
        enforce_exploit_scenario(parsed)

    return parsed


def validate_stage_output(parsed: dict, stage: str) -> dict:
    """Validate and normalize stage output structure.

    Ported from validateStageOutput() in shared/utils.js.
    """
    if not parsed or not isinstance(parsed, dict):
        return parsed

    if parsed.get("findings") is not None and not isinstance(parsed["findings"], list):
        parsed["findings"] = []
        parsed["_validation_warning"] = "findings was not an array; reset to []"
        logger.warning(f"[{stage}] findings was not an array; reset to []")

    if isinstance(parsed.get("findings"), list):
        before = len(parsed["findings"])
        parsed["findings"] = [
            f
            for f in parsed["findings"]
            if f and isinstance(f, dict) and isinstance(f.get("message"), str)
        ]
        dropped = before - len(parsed["findings"])
        if dropped > 0:
            logger.info(f"[{stage}] dropped {dropped} invalid findings")

    return parsed


def enforce_exploit_scenario(parsed: dict) -> None:
    """Downgrade critical/high findings without exploit scenarios.

    Findings with severity critical or high must have a substantive
    exploit_scenario (>=50 chars). Otherwise they're downgraded to medium.
    Ported from enforceExploitScenario() in extract-stage.js.
    """
    if not isinstance(parsed.get("findings"), list):
        return

    parsed["findings"] = [
        (
            {**f, "severity": "medium", "_downgraded": "missing concrete exploit_scenario"}
            if f.get("severity") in ("critical", "high")
            and len(f.get("exploit_scenario") or "") < 50
            else f
        )
        for f in parsed["findings"]
    ]


def build_extract_fallback(stage: str, raw_content: str = "") -> dict:
    """Build a fallback result when extraction fails entirely.

    Ported from buildFallback() in extract-stage.js.
    """
    return {
        "findings": [],
        "files_reviewed": [],
        "commands_run": [],
        "tests_written": [],
        "summary": (
            f"{stage} review completed but could not parse structured output. "
            "Auto-passing to avoid blocking."
        ),
        "pass": True,
        "error": "parse_failed",
        "raw_output": (raw_content or "")[:2000],
    }
