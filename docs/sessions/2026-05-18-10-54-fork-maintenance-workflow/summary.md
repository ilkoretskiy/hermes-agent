# Session: Fork Maintenance Workflow

**Date:** 2026-05-18
**Duration:** Medium
**Branch:** `planner5d/api-server-multi-model`

---

## 1. What this session was about

The session focused on making this fork's maintenance workflow explicit and neutral. The current workflow was tribal knowledge: pull upstream Hermes changes, merge them into a separate fixes branch, and run downstream production deployments from that fixes branch.

## 2. What changed

- `docs/plans/2026-05-14-neutral-fork-maintenance-design.md`: Captures the approved design for a neutral, fork-specific maintenance process.
- `docs/plans/2026-05-14-fork-maintenance-implementation.md`: Captures an implementation plan for branch renaming, documentation, and tests around upstream sync.
- `docs/sessions/2026-05-18-10-54-fork-maintenance-workflow/summary.md`: This summary.

No source code was changed. No tests were run because only planning and summary documents were added.

## 3. How the session unfolded

The user identified three problems with the current repository workflow: the process was not documented, the current fixes branch name `api-server-multi-model` no longer described its broader purpose, and the `hermes update` upstream-sync process was not mentioned or tested.

I inspected the repository context. The repo has `origin` pointing to the fork and `upstream` pointing to `NousResearch/hermes-agent`. The current branch is `planner5d/api-server-multi-model`. I found existing upstream-sync logic in `hermes_cli/main.py`, specifically `_sync_with_upstream_if_needed`, but did not find tests covering that helper. I also did not find a fork-specific workflow document.

We discussed whether the workflow should be private/fork-specific or a general Hermes feature. The user chose fork-specific, but wanted neutral language with no Planner5D references. The proposed neutral branch name became `fork/production-fixes`.

The first design covered upstream sync, but the user correctly pointed out that adding new local fixes was missing. We revised the model to include three branch roles: `main`, `fork/production-fixes`, and short-lived `fix/<topic>` branches. New fixes should branch from `fork/production-fixes` and merge back into it, not branch directly from `main`.

After the user approved the direction, I wrote the design document and then a detailed implementation plan. I did not rename the branch, write the final workflow doc, add tests, push, or commit anything.

## 4. Key decisions

1. The workflow should be fork-specific but neutral.
   It should avoid company, team, or deployment-specific naming while still documenting how this fork is maintained.

2. Rename the maintained branch conceptually to `fork/production-fixes`.
   The old branch name describes one historical feature, but the branch now carries multiple local fixes.

3. Keep `main` as the upstream-tracking branch.
   `main` should follow upstream Hermes as closely as possible.

4. Use `fork/production-fixes` as the downstream deployment branch.
   Deployments that need local fixes should pin to this maintained branch, not to `main`.

5. Add local fixes through short-lived `fix/<topic>` branches.
   New fixes should branch from `fork/production-fixes` so they include all currently carried fixes.

6. Add tests for existing upstream-sync behavior before relying on it further.
   `_sync_with_upstream_if_needed` already exists, but its behavior should be covered by mocked subprocess tests.

## 5. Key learnings

### About the user

- The user prefers reusable process documentation so they do not need to re-explain repository-specific workflows to future agents.
- The user wants neutral naming and wording, even for fork-specific operational docs.
- The user cares about the full maintenance lifecycle, including adding new local fixes, not just syncing upstream.

### About the system/project

- `hermes update` already has fork upstream-sync logic in `hermes_cli/main.py`.
- The upstream-sync helper is called only when the repo is detected as a fork and the active update branch is `main`.
- Existing tests cover many `cmd_update` behaviors, but not `_sync_with_upstream_if_needed` directly.
- `origin` is the fork remote and `upstream` is the official Hermes remote.

### About the process

- The important missing piece was not only documentation; it was separating two workflows: upstream sync and local fix development.
- Branch naming should describe role, not a single historical feature.

## 6. What's next

Start by deciding whether to execute the implementation plan in this session or another session.

Recommended next steps:

1. Rename local branch `planner5d/api-server-multi-model` to `fork/production-fixes`.
2. Push `origin/fork/production-fixes` and leave the old remote branch temporarily until consumers move.
3. Add `docs/fork-maintenance.md` using the neutral workflow from the plan.
4. Add tests for `_sync_with_upstream_if_needed` in `tests/hermes_cli/test_update_autostash.py`.
5. Run targeted update tests through `scripts/run_tests.sh`.
6. Move downstream deployments to `fork/production-fixes`.
7. Delete the old remote branch only after confirming no deployment still uses it.

## 7. Retrospective results

### Memories captured

No memory files were created. The reusable process was captured in repository docs/plans instead.

### Skills or rules proposed

No new skills or global rules were proposed. The fork workflow should live in `docs/fork-maintenance.md` rather than as a general assistant rule.

### Code opportunities identified

The only code opportunity identified was test coverage, not new automation: add subprocess-mocked tests for `_sync_with_upstream_if_needed`.

### Process improvements

The fork maintenance process should be documented as two separate workflows: syncing upstream and adding local fixes.

## 8. Artifacts index

| Type | File | Purpose |
|------|------|---------|
| Design | `docs/plans/2026-05-14-neutral-fork-maintenance-design.md` | Approved neutral design for fork maintenance |
| Plan | `docs/plans/2026-05-14-fork-maintenance-implementation.md` | Step-by-step implementation plan |
| Summary | `docs/sessions/2026-05-18-10-54-fork-maintenance-workflow/summary.md` | Self-contained recap of the thread |
