# Finding schema

Canonical shape of a review finding in Gate. The source of truth is the
`Finding` dataclass in [`gate/schemas.py`](../gate/schemas.py); this
document describes the contract so stage authors and operators know
which fields they can rely on.

## Why a dataclass?

Before PR A, the finding shape was tribal knowledge: every renderer
(`github._format_findings`, `fixer_polish._render_single_finding_prompt`,
`finding_id.compute_finding_id`) read a loose `dict` with `.get("field",
default)` calls. That worked but meant:

- Human post-mortems reached for reasonable-sounding field names
  (`title`, `description`) that happened not to be populated by any
  stage today.
- Emitter drift (e.g. a stage forgetting `severity`) silently dropped
  the finding instead of raising at render time.

`Finding.from_dict(raw)` is the single normalisation path. It validates
required fields, coerces stringy numerics, and preserves unknown keys
in an `extra` bucket so future stage additions round-trip without being
dropped.

## Required fields

Every finding emitted by a review stage MUST populate these:

| Field      | Type  | Notes                                                              |
| ---------- | ----- | ------------------------------------------------------------------ |
| `severity` | `str` | One of `info` / `warning` / `error` / `critical`                   |
| `file`     | `str` | Repo-relative path                                                 |
| `message`  | `str` | Primary human-readable description. Renderers show this verbatim.  |

Missing or empty required fields cause `Finding.from_dict` to raise
`ValueError`, which surfaces as a visible "Malformed findings" section
in PR comments — the error is loud on purpose.

## Optional fields

| Field              | Type              | Notes                                                                              |
| ------------------ | ----------------- | ---------------------------------------------------------------------------------- |
| `line`             | `int`             | 1-based line number                                                                |
| `column`           | `int`             | 1-based column                                                                     |
| `title`            | `str`             | Short label. Reserved — review prompts don't emit it today. Future-use.            |
| `rule_source`      | `str`             | Rule/check name (e.g. `"style §8"`, `"@next/next/no-html-link-for-pages"`).        |
| `suggestion`       | `str`             | Fix recommendation. Rendered under `> Fix:` in PR comments.                        |
| `category`         | `str`             | Coarse bucket (`"security"`, `"performance"`, `"style"`, ...).                     |
| `source_stage`     | `str`             | Which review stage produced the finding. Stamped by the verdict agent.             |
| `introduced_by_pr` | `bool \| null`    | True when the cited line lives inside this PR's diff hunks.                        |
| `evidence_level`   | `str`             | `test_confirmed` / `code_trace` / `pattern_match` / `speculative`.                 |
| `finding_id`       | `str`             | SHA-1 prefix. Stamped by the orchestrator after verdict (see `finding_id.py`).    |
| `locations`        | `list[dict]`      | PR A.2 dedup output. See below.                                                    |

## The `locations` array (PR A.2 dedup)

`_dedupe_findings` in `gate/extract.py` merges findings that share
`(source_stage, rule_source, normalised_message)` into a single finding
with a `locations` array:

```json
{
  "severity": "warning",
  "file": "gate/builder.py",
  "line": 159,
  "message": "Multi-line comment block (style §8)",
  "rule_source": "style §8",
  "source_stage": "architecture",
  "locations": [
    {"file": "gate/builder.py", "line": 159},
    {"file": "gate/builder.py", "line": 235},
    {"file": "gate/builder.py", "line": 293}
  ]
}
```

Consumers that only want the primary site (legacy comment renderer,
`finding_id` hash input) use `locations[0]`, which is kept in sync with
the top-level `file`/`line` fields. Renderers that want every site
(polish prompt, PR comment) iterate `locations`.

Findings without a `locations` key (pre-PR-A state files,
externally-synthesised findings) are handled as a single site:
`Finding.iter_locations()` synthesises a one-element list so every
caller can iterate uniformly.

## Per-stage emitter checklist

Each review stage is responsible for producing findings matching this
shape. The full payload is documented in each stage's prompt, but the
required-field contract is the same across all of them:

- [`prompts/architecture.md`](../prompts/architecture.md) — emits
  `severity` / `file` / `message` / `line` / `rule_source` /
  `suggestion` / `category` / `introduced_by_pr` / `evidence_level`.
- [`prompts/logic.md`](../prompts/logic.md) — same core fields plus
  logic-specific evidence metadata.
- [`prompts/security.md`](../prompts/security.md) — same plus optional
  `exploit_scenario` (enforced by `enforce_exploit_scenario` in
  `extract.py`).
- [`prompts/verdict.md`](../prompts/verdict.md) — aggregates findings
  from the prior three stages and stamps `source_stage`.

## Inspecting findings

Use `gate inspect-pr <N> [--repo <owner/name>]` to pretty-print a PR's
persisted findings, or `gate inspect-pr <N> --raw` to dump the raw
JSON. This is the command to reach for during post-mortems — it goes
through `Finding.from_dict`, so malformed findings show up explicitly
instead of silently rendering as blank cells.
