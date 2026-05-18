# Fork Maintenance Agent Runbook

Use this file as an agent instruction. If the user says "follow `docs/fork-maintenance.md`", perform the requested workflow from this document end-to-end unless a stop condition is hit.

## Objective

Maintain a fork that carries local production fixes while regularly importing upstream Hermes changes.

## Branch Roles

- `main`: follows upstream Hermes as closely as possible.
- `fork/production-fixes`: maintained branch used by downstream deployments; contains `main` plus local fixes.
- `fix/<topic>`: short-lived branch for one local fix, created from `fork/production-fixes`.

## Agent Rules

- Treat `origin` as the fork remote.
- Treat `upstream` as the official Hermes remote.
- Do not develop local fixes directly on `main`.
- Do not deploy or recommend deploying from `main` when local fixes are required.
- Do not delete old remote branches unless the user explicitly confirms downstream consumers moved.
- Do not force-push `fork/production-fixes`.
- If a downstream project pins this repository by commit hash, report the final verified `fork/production-fixes` commit hash in the final response.

## Stop Conditions

Stop and ask the user before continuing if any of these happen:

- The working tree has unrelated uncommitted changes.
- `origin` or `upstream` is missing or points to an unexpected repository.
- `main` has fork-only commits that are not in `upstream/main`.
- `fork/production-fixes` is missing locally and on `origin`.
- A merge conflict is risky or cannot be resolved confidently.
- Verification fails and the fix is not obvious.
- Pushing to `origin/main` or `origin/fork/production-fixes` is rejected.

## Workflow: Sync Upstream Into The Maintained Fork Branch

Use this workflow when official Hermes changes should be loaded into the maintained fork branch.

### 1. Preflight

Run:

```bash
git status --short --branch
git remote -v
git branch --show-current
```

Expected:

- The working tree is clean, or only contains changes that belong to this maintenance task.
- `origin` is the fork.
- `upstream` is the official Hermes repository.

If the working tree has unrelated changes, stop and ask the user how to proceed.

### 2. Fetch Remotes

Run:

```bash
git fetch origin
git fetch upstream main
```

Expected: both fetches succeed.

### 3. Update `main` From Upstream

Run:

```bash
git checkout main
git merge --ff-only upstream/main
git push origin main
```

Expected:

- `main` fast-forwards to `upstream/main`.
- `origin/main` is updated.

If `git merge --ff-only upstream/main` fails, stop. Do not create fork-only merge commits on `main` unless the user explicitly approves.

### 4. Update `fork/production-fixes`

Run:

```bash
git checkout fork/production-fixes
git pull --ff-only origin fork/production-fixes
git merge main
```

Expected:

- `fork/production-fixes` is current with its remote before merging.
- `main` is merged into `fork/production-fixes`.

If there are conflicts, resolve them only when the correct resolution is clear. After resolving conflicts, run:

```bash
git status --short
git add <resolved-files>
git commit
```

If the merge completes automatically, no extra commit command is needed beyond the merge commit or fast-forward that Git creates.

### 5. Verify

Run targeted tests for affected areas. At minimum for update/fork-sync changes, run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_update_autostash.py tests/hermes_cli/test_cmd_update.py -v
```

When time allows before publishing a broadly consumed commit, run:

```bash
scripts/run_tests.sh
```

If tests fail, fix the issue if it is clearly part of this merge. Otherwise stop and report the failure.

### 6. Push Maintained Branch

Run:

```bash
git push origin fork/production-fixes
```

Expected: push succeeds without force.

### 7. Report Final Hash

Run:

```bash
git rev-parse fork/production-fixes
git status --short --branch
```

The final response must include:

```text
Downstream commit hash: <full commit hash>
```

Also report which tests were run and whether the old remote branch, if any, was left in place.

## Workflow: Add A Local Fix

Use this workflow when a new local fix is needed for downstream production use.

### 1. Branch From The Maintained Fork Branch

Run:

```bash
git checkout fork/production-fixes
git pull --ff-only origin fork/production-fixes
git checkout -b fix/<topic>
```

Expected: the fix branch starts from the current maintained branch.

### 2. Implement And Test

Make the minimal fix. Add or update tests for the changed behavior.

Run targeted tests with `scripts/run_tests.sh`, not raw `pytest`.

### 3. Merge Back Into The Maintained Branch

Run:

```bash
git checkout fork/production-fixes
git merge --no-ff fix/<topic>
scripts/run_tests.sh <targeted-tests>
git push origin fork/production-fixes
```

If the fix branch should be kept for review, ask before deleting it. If the user wants it removed after merge, run:

```bash
git branch -d fix/<topic>
git push origin --delete fix/<topic>
```

### 4. Report Final Hash

Run:

```bash
git rev-parse fork/production-fixes
```

The final response must include:

```text
Downstream commit hash: <full commit hash>
```

## Downstream Project Updates

Downstream deployments should pin to `fork/production-fixes`, not `main`.

If a downstream project references this repository by commit hash:

1. Use the final hash from `git rev-parse fork/production-fixes`.
2. Replace the old hash in the downstream project.
3. Run the downstream project's relevant checks if available.
4. Report any downstream project that could not be updated or verified.

## Old Branch Cleanup

Only after the user confirms all downstream consumers moved away from the old branch name, delete the old branch.

Example:

```bash
git push origin --delete planner5d/api-server-multi-model
git fetch origin --prune
```

Do not run this cleanup automatically.
