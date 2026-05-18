# Fork Maintenance

This fork keeps a maintained branch for downstream deployments while continuing to pull upstream Hermes changes into `main`.

## Branch Roles

- `main`: follows upstream Hermes as closely as possible.
- `fork/production-fixes`: maintained branch used by downstream deployments; contains `main` plus local fixes.
- `fix/<topic>`: short-lived branch for one local fix, created from `fork/production-fixes`.

## Sync Upstream

Use this workflow when official Hermes changes arrive.

1. Checkout `main`.
2. Fetch or pull `upstream/main` into `main`.
3. Push updated `main` to `origin/main`.
4. Checkout `fork/production-fixes`.
5. Merge updated `main` into `fork/production-fixes`.
6. Resolve conflicts if needed.
7. Run targeted tests for affected areas.
8. Run `scripts/run_tests.sh` before publishing when feasible.
9. Push `fork/production-fixes`.

## Add A Local Fix

Use this workflow when a new local fix is needed for downstream production use.

1. Checkout `fork/production-fixes`.
2. Create a short-lived branch, for example `fix/api-server-model-routing`.
3. Implement the fix.
4. Add or update tests.
5. Run targeted tests.
6. Merge the branch back into `fork/production-fixes`.
7. Verify the maintained branch.
8. Push `fork/production-fixes`.

New fixes should branch from `fork/production-fixes`, not from `main`, so they are developed and tested on top of all currently carried local fixes.

## Deployment Rule

Downstream deployments should pin to `fork/production-fixes`, not `main`.

If a downstream project references this repository by commit hash, update that reference to the final verified `fork/production-fixes` commit hash after maintenance work is complete.

## Safety Notes

- Do not develop new local fixes directly on `main`.
- Do not deploy directly from `main` if local fixes are required.
- Keep old remote branch names temporarily when renaming deployment branches.
- Resolve upstream merge conflicts on `fork/production-fixes`, then test the affected areas.
- Delete old remote branch names only after confirming downstream consumers moved.
