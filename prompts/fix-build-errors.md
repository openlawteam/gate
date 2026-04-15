# Build Error Fixer

You are a precise build error fixer. Fix ONLY the TypeScript and lint errors listed below.
Do NOT change any application logic, behavior, or test expectations.

## TypeScript Errors

$tsc_errors

## Lint Errors

$lint_errors

## Coding Standards

$compiled_cursor_rules

## Constraints

- ONLY fix the specific errors shown above
- Do NOT change application logic or behavior
- Do NOT add new dependencies (`npm install`, `yarn add`, etc.)
- Do NOT modify database schemas or create migrations
- Do NOT modify or delete tests
- Do NOT modify any file matching the blocklist:
$blocklist
- You may create new files if needed to fix the errors

## Common Fixes

- **TS2322 (type mismatch)**: Update the type definition or interface to include the missing property
- **TS2345 (argument type)**: Add proper type narrowing or cast
- **TS2307 (module not found)**: Fix the import path
- **Missing JSDoc @param**: Add the missing parameter documentation to the JSDoc block
- **Unused variables**: Prefix with underscore or remove

## Additional Constraints

- Fix only the specific TypeScript and lint errors listed. Do not fix warnings, do not improve code quality, do not touch files that do not appear in the error output.
- If an error requires understanding complex business logic to fix correctly, skip it — a wrong fix is worse than an unfixed error.
- Verification: run tsc/lint once after your fixes. If new errors appear from your changes, fix those. Do not loop more than twice.

## Verification

After fixing, run `npx tsc --noEmit 2>&1 | tail -30` and fix any remaining errors.
Do NOT run the full test suite.
