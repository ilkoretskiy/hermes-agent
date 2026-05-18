# Session: Fork Maintenance Workflow

**Date:** 2026-05-18
**Duration:** Medium
**Branch:** `fork/production-fixes`

---

## 1. What this session was about

The session focused on making this fork's maintenance workflow explicit and neutral. The current workflow was tribal knowledge: pull upstream Hermes changes, merge them into a separate fixes branch, and run downstream production deployments from that fixes branch.

## 2. What changed

- `docs/plans/2026-05-14-neutral-fork-maintenance-design.md`: Captures the approved design for a neutral, fork-specific maintenance process.
- `docs/plans/2026-05-14-fork-maintenance-implementation.md`: Captures an implementation plan for branch renaming, documentation, and tests around upstream sync.
- `docs/fork-maintenance.md`: Documents the neutral fork-maintenance workflow, including upstream sync, local-fix branches, and downstream commit-hash updates.
- `README.md`: Links maintainers to the fork-maintenance document.
- `tests/hermes_cli/test_update_autostash.py`: Adds mocked tests for `_sync_with_upstream_if_needed` and isolates update tests from the stale-dashboard warning subprocess scan.
- `docs/sessions/2026-05-18-10-54-fork-maintenance-workflow/summary.md`: This summary.

The maintained branch was renamed locally to `fork/production-fixes` and pushed to `origin/fork/production-fixes`. The old remote branch `origin/planner5d/api-server-multi-model` was intentionally left in place until downstream consumers move.

## 3. How the session unfolded

The user identified three problems with the current repository workflow: the process was not documented, the current fixes branch name `api-server-multi-model` no longer described its broader purpose, and the `hermes update` upstream-sync process was not mentioned or tested.

I inspected the repository context. The repo has `origin` pointing to the fork and `upstream` pointing to `NousResearch/hermes-agent`. The current branch is `planner5d/api-server-multi-model`. I found existing upstream-sync logic in `hermes_cli/main.py`, specifically `_sync_with_upstream_if_needed`, but did not find tests covering that helper. I also did not find a fork-specific workflow document.

We discussed whether the workflow should be private/fork-specific or a general Hermes feature. The user chose fork-specific, but wanted neutral language with no Planner5D references. The proposed neutral branch name became `fork/production-fixes`.

The first design covered upstream sync, but the user correctly pointed out that adding new local fixes was missing. We revised the model to include three branch roles: `main`, `fork/production-fixes`, and short-lived `fix/<topic>` branches. New fixes should branch from `fork/production-fixes` and merge back into it, not branch directly from `main`.

After the user approved the direction, I wrote the design document and then a detailed implementation plan. The user later asked to include downstream projects that pin the repo by commit hash, so the plan and final workflow doc now require reporting the final verified `fork/production-fixes` hash.

The implementation was then executed directly in this session without subagents. I added the neutral workflow doc, linked it from the README, added tests for the upstream-sync helper, fixed the existing update test setup to avoid an unrelated stale-dashboard subprocess scan, renamed the local branch to `fork/production-fixes`, committed the changes, and pushed the new branch.

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

7. Downstream projects that pin by commit hash need the final verified branch hash.
   The final response should include `Downstream commit hash: <hash>` so those projects can update their references.

## 5. Key learnings

### About the user

- The user prefers reusable process documentation so they do not need to re-explain repository-specific workflows to future agents.
- The user wants neutral naming and wording, even for fork-specific operational docs.
- The user cares about the full maintenance lifecycle, including adding new local fixes, not just syncing upstream.

### About the system/project

- `hermes update` already has fork upstream-sync logic in `hermes_cli/main.py`.
- The upstream-sync helper is called only when the repo is detected as a fork and the active update branch is `main`.
- Existing tests cover many `cmd_update` behaviors, and this session added direct coverage for `_sync_with_upstream_if_needed`.
- `origin` is the fork remote and `upstream` is the official Hermes remote.

### About the process

- The important missing piece was not only documentation; it was separating two workflows: upstream sync and local fix development.
- Branch naming should describe role, not a single historical feature.

## 6. What's next

Recommended next steps:

1. Update downstream projects that pin this repository by commit hash to the final `fork/production-fixes` hash reported at completion.
2. Move any downstream branch references from `planner5d/api-server-multi-model` to `fork/production-fixes`.
3. Delete the old remote branch only after confirming no deployment still uses it.
4. Run the full test suite before broader publishing if needed; this session ran the targeted update tests only.

## 7. Retrospective results

### Memories captured

No memory files were created. The reusable process was captured in repository docs/plans instead.

### Skills or rules proposed

No new skills or global rules were proposed. The fork workflow should live in `docs/fork-maintenance.md` rather than as a general assistant rule.

### Code opportunities identified

The only code opportunity identified was test coverage, not new automation. This session added subprocess-mocked tests for `_sync_with_upstream_if_needed`.

### Process improvements

The fork maintenance process should be documented as two separate workflows: syncing upstream and adding local fixes.

## 8. Artifacts index

| Type | File | Purpose |
|------|------|---------|
| Design | `docs/plans/2026-05-14-neutral-fork-maintenance-design.md` | Approved neutral design for fork maintenance |
| Plan | `docs/plans/2026-05-14-fork-maintenance-implementation.md` | Step-by-step implementation plan |
| Documentation | `docs/fork-maintenance.md` | Neutral fork maintenance workflow |
| Documentation | `README.md` | Link to fork maintenance docs |
| Tests | `tests/hermes_cli/test_update_autostash.py` | Upstream-sync helper coverage |
| Summary | `docs/sessions/2026-05-18-10-54-fork-maintenance-workflow/summary.md` | Self-contained recap of the thread |
