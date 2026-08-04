# Security Policy

## Supported versions

Gate is pre-1.0. Security fixes are applied to the latest released version. The v0.1 runtime support boundary is macOS; Apple Silicon on macOS 26.5 is owner-verified.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability.

Use GitHub's **Report a vulnerability** action on the repository Security tab to open a private security advisory. Include:

- the affected Gate version or commit;
- the macOS version and architecture;
- reproduction steps and the security impact;
- whether credentials, fork trust, subprocess execution, worktrees, or auto-fix are involved;
- any proposed mitigation, if known.

Maintainers will acknowledge a complete report as soon as practical, coordinate validation privately, and publish remediation details after a fix is available. Please avoid accessing data or repositories you do not own while testing.

## v0.1 boundary

Gate executes only same-repository pull requests. Fork execution is unsupported and has no configuration bypass. Auto-fix is disabled unless a repository explicitly sets `auto_fix = true`. Infrastructure failure paths must never approve a pull request.
