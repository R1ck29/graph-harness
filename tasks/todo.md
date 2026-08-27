# Plan: install the harness for Codex and Claude Code on this PC

## Discovery and design

- [x] Inventory the installed Codex and Claude Code versions, global discovery
      paths, and existing harness-related configuration without exposing secrets.
- [x] Map the repository's canonical skills, agents, CLI, and guard into a single
      source-of-truth installation layout that does not duplicate existing files.
- [x] Define backup, upgrade, uninstall, and rollback behavior before mutating
      global configuration.

## Implementation

- [x] Add or repair an idempotent local installer if the repository cannot be
      installed directly into both clients as-is.
- [x] Add characterization and installer tests before changing installation
      behavior.
- [x] Install the Python CLI and both client adapters on this PC while preserving
      unrelated user configuration.

## Verification

- [x] Verify Codex discovers the installed workflow and reviewer in a clean
      temporary project and completes a real graph review path.
- [x] Verify Claude Code discovers the installed workflow and reviewer in a clean
      temporary project and completes a real graph review path.
- [x] Run the complete unit, formatting, typing, adapter-sync, security, and
      installation test suite.
- [x] Review the final diff and installed state independently; resolve all
      critical and high findings.
- [x] Commit and push repository changes after all checks pass.

## Review

- The installer is idempotent and read-only in `--check` mode, rejects target
  conflicts before creating a runtime, builds from an isolated staging copy,
  preserves unrelated configuration, and removes only exact managed entries.
- The PC install is in sync. Both clients reference one shared canonical skill
  payload, while their reviewer profiles remain regular client-specific files.
  Codex and Claude instruction files each have one managed block, and Claude has
  one shell-free Stop hook entry.
- Real clean-project E2E runs passed with Codex CLI 0.146.1, desktop Codex
  0.150.0-alpha.8, and Claude Code 2.1.239. Both clients discovered the global
  workflow and delegated independent verification successfully.
- Automated verification passed: 115 tests (2 Windows-only skips), Black
  26.5.1 across 22 files, strict mypy across 22 files, adapter sync, five-case
  offline evaluation, Claude adapter validation, compileall, installer drift
  check, and whitespace validation.
- Independent code, security, and Python reviews report no remaining CRITICAL,
  HIGH, or MEDIUM findings.

### Discovery record

- Installed clients: Codex CLI 0.146.1 in the login shell (the desktop bundle
  carries 0.150.0-alpha.8) and Claude Code 2.1.239.
- The current adapter is project-scoped: its skills, reviewers, and Stop hook
  refer to files under the opened repository, and `graphctl` is not globally
  installed. Direct copying would fail in an unrelated checkout.
- The integration will use one managed payload, symlink both client skill trees
  to that payload on this Mac, merge one marked instruction block per client,
  merge one Claude Stop hook, preserve unrelated configuration, and provide
  check/uninstall behavior with backups for displaced same-name targets.
