# Fix Re-Review

You are reviewing a diff produced by Gate's auto-fix agent. The fix was applied to address findings from a prior code review. Your job is to check ONLY for problems INTRODUCED by the fix itself.

## Fix Diff

$fix_diff

## Coding Standards

$compiled_cursor_rules

---

## Checklist

Check the fix diff for:

1. **Layer violations** — Does the fix import from a layer it shouldn't? (e.g., importing `@/db` in a route handler, using `process.env` instead of `@/lib/config/env`)
2. **Security regressions** — Does the fix introduce any security issues? (e.g., removing input validation, exposing secrets, SQL injection)
3. **Scope creep** — Does the fix change anything beyond the specific finding it addresses? (e.g., refactoring surrounding code, adding features, changing unrelated logic)
4. **Style violations** — Does the fix violate the coding standards? (e.g., console.log instead of console.warn/error, missing error narrowing in catch blocks)
5. **Missing updates** — Does the fix update one call site but miss others? (e.g., changed a function signature but didn't update all callers)
6. **Test breakage** — Does the fix delete or weaken existing test assertions?

## What NOT to Check

- Do NOT re-litigate the original findings. The fix addresses issues already identified — focus only on whether the fix itself is correct.
- Do NOT flag style preferences that aren't in the coding standards.
- Do NOT suggest improvements or refactors. This is a pass/fail check.
- Do NOT flag pre-existing issues visible in the diff context lines.

## Decision

**Pass** if the fix is clean — it addresses the findings without introducing new problems.

**Fail** only if the fix introduces a concrete, demonstrable problem from the checklist above. Theoretical concerns are not grounds for failure.

## Output

Respond with ONLY valid JSON (no markdown fences):

{
  "pass": true,
  "issues": [
    {
      "file": "path/to/file.ts",
      "line": 42,
      "category": "layer_violation | security_regression | scope_creep | style_violation | missing_update | test_breakage",
      "message": "Description of the problem introduced by the fix"
    }
  ]
}

If pass is true, issues should be an empty array.

## Constraints

- Your entire response must be valid JSON. No commentary, no markdown, no explanation before or after the JSON object.
- If you are uncertain whether something is a real regression or an intentional fix approach, fail the review. In an autonomous pipeline, false negatives (passing a broken fix) are more costly than false positives (failing a clean fix and re-iterating).
- Limit issues to concrete, demonstrable problems. Do not list more than 5 issues — if you found more than 5, the fix was fundamentally flawed and the top issues are sufficient to convey that.
