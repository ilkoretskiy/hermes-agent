# Neutral Fork Maintenance Design

## Context

This repository is a fork of upstream Hermes. The fork carries local fixes that are needed by downstream production deployments, while upstream Hermes continues to move independently.

The current process exists mostly as tribal knowledge:

- pull changes from upstream Hermes
- merge those changes into a separate branch that carries local fixes
- run production deployments from that maintained fixes branch

That process should be documented in neutral, fork-specific terms so future agents and maintainers do not need it re-explained.

## Goals

- Document the fork maintenance workflow without naming any company, team, or deployment.
- Rename the current fixes branch to a neutral name that reflects its role.
- Keep upstream synchronization and local fix development as separate workflows.
- Add test coverage for the existing `hermes update` upstream-sync behavior.

## Branch Roles

Use three branch categories:

- `main`: clean fork branch that follows upstream Hermes as closely as possible.
- `fork/production-fixes`: maintained branch used by downstream deployments; contains `main` plus local carried fixes.
- `fix/<topic>`: short-lived branches created from `fork/production-fixes` for new local fixes.

The current branch `planner5d/api-server-multi-model` should be renamed to `fork/production-fixes` because it now carries more than one fix and should not encode a single feature or organization name.

## Workflow: Sync Upstream

Use this when official Hermes changes arrive.

1. Checkout `main`.
2. Fetch or pull `upstream/main` into `main`.
3. Push updated `main` to `origin/main`.
4. Checkout `fork/production-fixes`.
5. Merge updated `main` into `fork/production-fixes`.
6. Resolve conflicts if needed.
7. Run targeted tests for affected areas.
8. Run the full test suite before publishing or deploying when feasible.
9. Push `fork/production-fixes`.

Downstream deployments should continue using `fork/production-fixes`, not `main`, because `main` does not include carried local fixes.

## Workflow: Add Local Fix

Use this when a new fix is needed for downstream production use.

1. Checkout `fork/production-fixes`.
2. Create a short-lived branch from it, for example `fix/api-server-model-routing`.
3. Implement the fix on that branch.
4. Add or update tests for the fix.
5. Run targeted tests.
6. Merge the short-lived branch back into `fork/production-fixes`.
7. Run the relevant verification for the maintained branch.
8. Push `fork/production-fixes`.

New fixes should branch from `fork/production-fixes`, not from `main`, so they are developed and tested on top of all currently carried local fixes.

## Workflow: Deployment Consumption

Downstream deployments should pin to `fork/production-fixes`.

`main` is for upstream tracking. It may be valid upstream Hermes code, but it is not the branch that includes local production fixes.

## Code And Test Changes

The existing `hermes update` command already contains upstream-sync logic in `hermes_cli/main.py`, but that path is not covered by tests today.

Add tests for `_sync_with_upstream_if_needed` covering:

- missing `upstream` remote prompt path
- failed upstream fetch
- fork has commits not present upstream, so sync is skipped
- upstream has no new commits, so sync is skipped
- upstream is ahead and the fork can fast-forward safely
- successful upstream pull followed by fork push attempt

The tests should mock git subprocess calls rather than requiring real remotes.

## Documentation Changes

Add a neutral fork maintenance document, for example `docs/fork-maintenance.md`, with:

- branch roles
- upstream sync workflow
- local fix workflow
- deployment branch rule
- verification expectations
- safety notes for conflict handling and stale branches

Optionally link it from `README.md` under contributor or maintainer documentation, but keep it clearly scoped to fork maintenance rather than upstream Hermes contribution policy.

## Open Decisions

- Whether to rename only the local branch first, or also push the new remote branch and delete the old remote branch after downstream consumers move.
- Whether `docs/fork-maintenance.md` should live only in this fork or be suitable for upstream contribution as a generic fork guide.
