# Fix Plan Refine

You are validating and refining a fix plan produced by the planning phase. Your job is to read the files cited in the plan, verify that the plan's assumptions are correct, and produce a refined implementation spec.

## Original Plan

$fix_plan

## Findings

$findings_json

## Coding Standards

$compiled_cursor_rules

## Blocked Files (DO NOT plan changes to these)

$blocklist

## Your Process

1. **Read every file cited in the plan** — verify that the functions, exports, imports, and line numbers referenced in the plan actually exist where the plan says they do.
2. **Check assumptions** — does the plan assume a file structure, export pattern, or naming convention that doesn't match reality? Flag any mismatches.
3. **Verify dependencies** — is the step ordering correct? Will step 3's changes break step 5? Are there circular dependencies the plan missed?
4. **Identify gaps** — are there callers or consumers the plan forgot to update? Will the planned changes break any imports that aren't listed?
5. **Refine** — produce a corrected plan with accurate file paths, line references, and dependency ordering. Keep the same structure but fix any inaccuracies.

## What NOT to Do

- Do NOT implement any changes — only read and verify
- Do NOT add new findings or expand scope beyond what the original plan covers
- Do NOT remove plan steps unless they target nonexistent files or would break the build
- Do NOT modify any file matching the blocklist

## Constraints

- Limit yourself to 15 file reads. Focus on files directly cited in plan steps.
- If the original plan is accurate and needs no changes, return it unchanged with a note that validation passed.
- If a plan step references a file that doesn't exist, mark it with `"status": "invalid"` and explain why.

## Output

Respond with ONLY valid JSON (no markdown fences):

{
  "plan": [
    {
      "step": 1,
      "finding_file": "path/to/file.ts",
      "finding_line": 42,
      "action": "Corrected action description with accurate references",
      "files_to_modify": ["actual/paths.ts"],
      "files_to_create": ["new/file.ts"],
      "dependent_updates": ["Updated dependency list"],
      "risk": "low",
      "status": "verified",
      "refinement_notes": "Original plan was accurate / Changed X because Y"
    }
  ],
  "skipped": [
    { "finding": "description", "reason": "blocked_file | invalid_reference | insufficient_context" }
  ],
  "execution_notes": "Updated ordering notes",
  "validation_summary": "Brief summary of what was verified and what changed"
}
