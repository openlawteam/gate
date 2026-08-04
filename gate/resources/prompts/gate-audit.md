# Code Review

Review the area described below thoroughly. Trace through all logic paths until you have the complete picture. Find:

- **Dead/unused code** — functions, imports, variables, files with no callers
- **Dangling references** — broken imports, missing deps, orphaned calls after refactors
- **Stale/outdated content** — comments, naming that doesn't match current code
- **Redundancy** — DRY violations, duplicate logic that should be consolidated
- **Missing updates** — changed a function signature but didn't update all callers
- **Forgotten barrel re-exports** — if code was extracted, the original barrel must re-export
- **Unused imports** — stale imports left behind after refactoring

For each finding, note: what, where (file:line), and why it's an issue.

## Output

Present a categorized summary of issues found:

- Group by category, not by file
- Prioritize by severity (critical first)
- Surface any questions needing clarification

Do not implement fixes yet — findings only for review and approval.

---

## Directions

$request
