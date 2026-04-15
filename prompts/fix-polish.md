# Fix Polish

Review the changes made by the fix agent. Look for minor issues introduced by the fixes themselves. You may make small corrections.

## Changes Made

$fix_diff

## Coding Standards

$compiled_cursor_rules

## What to Check

- **Missing imports or exports** after file splits — new files need to be imported where used
- **Dangling references** to moved/renamed functions — callers must be updated
- **Inconsistent naming** between new and existing code — follow existing conventions
- **Forgotten barrel re-exports** in index files — if code was extracted, the original barrel must re-export
- **Unused imports** left behind after refactoring — clean up stale imports
- **Missing JSDoc** on new public functions — follow the project's documentation conventions

## What NOT to Do

- Do NOT change application logic
- Do NOT fix findings that were not part of this fix session
- Do NOT refactor code that works correctly
- Do NOT add new features or improvements
- Do NOT modify any file matching the blocklist (`.github/**`, `.env*`, `src/db/schema/**`, `drizzle/**`, `package.json`, `package-lock.json`, `tsconfig.json`, `.cursor/rules/**`)

## Constraints

- Make at most 10 corrections. If you find more issues than this, the fix itself was insufficient — list the 10 most important corrections and stop.
- Each correction should be a small, mechanical change (missing import, stale reference, naming inconsistency). If a "correction" requires understanding business logic or changing behavior, it is not a polish item — skip it.
- Do not read files that were not changed in the fix diff. Your scope is the diff, not the broader codebase.

## After Corrections

Run `npx tsc --noEmit 2>&1 | tail -20` to verify your corrections compile.

## Output

Respond with ONLY valid JSON (no markdown fences):

{
  "corrections": [
    { "file": "path/to/file.ts", "description": "Added missing import for ExtractedComponent" }
  ],
  "clean": false
}

If no corrections needed, set `"clean": true` and `"corrections": []`.
