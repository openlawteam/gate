# Fix Plan

> **RESERVED (not live).** This prompt is kept in-tree for reference and
> for future deep-plan experiments. It is **not** part of the current
> fix pipeline, which is driven by `fix-senior.md` (monolithic path)
> and the per-finding polish loop in `gate.fixer_polish`. Do not rely
> on this file during a live fix run — edits here do not affect
> production behavior.

Create a concrete, ordered action plan to fix the findings below. You have codebase context from a prep phase — use it to plan accurate, dependency-aware fixes.

## Findings

$findings_json

## Codebase Context (from prep phase)

$prep_context

## Blocklist (DO NOT plan changes to these)

$blocklist

## Coding Standards

$compiled_cursor_rules

## Requirements

1. **Order by dependency** — if fix A creates a file that fix B imports, A must come first. If fix A changes an export signature, updating callers comes after.

2. **Be specific** — for each fix, specify the exact file path, what to change, why, and any files that need updating as a consequence.

3. **Plan new files** — if a fix requires creating new files (component extraction, helper modules), list the new file path and describe what it contains.

4. **Respect the blocklist** — if a fix requires modifying a blocked file, mark it as skipped with reason `blocked_file`.

5. **Group atomically** — related fixes that must be applied together should be grouped in the same step.

6. **Skip info-severity** — only plan fixes for critical, error, and warning findings.

Planning only — do NOT implement any changes.

## Constraints

- Do not read files. Use the prep context provided above. If the prep context is missing information needed for a finding, skip that finding with reason "insufficient_context".
- Limit the plan to 10 steps maximum. If more than 10 steps are needed, prioritize critical and error findings and skip warnings with reason "deferred_to_next_cycle".
- Each step should touch at most 5 files. If a single finding requires modifying more than 5 files, mark it as a separate high-risk step with a note about the blast radius.

## Output

Respond with ONLY valid JSON (no markdown fences):

{
  "plan": [
    {
      "step": 1,
      "finding_file": "path/to/source.file",
      "finding_line": 42,
      "action": "Extract SummaryScreen component to SharedDeckSummaryScreen (new module)",
      "files_to_modify": ["SharedDeckStudyPreview"],
      "files_to_create": ["SharedDeckSummaryScreen"],
      "dependent_updates": ["Update import in page module"],
      "risk": "low"
    }
  ],
  "skipped": [
    { "finding": "description of finding", "reason": "blocked_file" }
  ],
  "execution_notes": "Apply steps 1-3 before step 4 (step 4 depends on exports from step 2)"
}
