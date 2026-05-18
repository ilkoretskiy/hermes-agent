# Fork Maintenance Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Document and test a neutral fork-maintenance workflow with a maintained production-fixes branch and explicit local-fix workflow.

**Architecture:** Keep the fork process as documentation plus small regression tests around the existing update code. Do not add new automation until the current `hermes update` upstream-sync behavior is documented and tested.

**Tech Stack:** Markdown docs, Git branch operations, Python `pytest` tests run through `scripts/run_tests.sh`, existing `hermes_cli.main` update helpers.

---

### Task 1: Rename The Maintained Fixes Branch

**Files:**
- No file edits
- Git branch state only

**Step 1: Inspect current branch state**

Run: `git status --short --branch`

Expected: current branch is the old maintained fixes branch, currently `planner5d/api-server-multi-model`, with no blocking uncommitted changes that would be affected by a branch rename.

**Step 2: Rename the local branch**

Run: `git branch -m fork/production-fixes`

Expected: local branch is renamed.

**Step 3: Push the new remote branch**

Run: `git push -u origin fork/production-fixes`

Expected: remote branch `origin/fork/production-fixes` exists and local branch tracks it.

**Step 4: Keep the old remote branch until consumers move**

Do not delete `origin/planner5d/api-server-multi-model` in this task. Keep it temporarily so downstream deployments can move deliberately.

**Step 5: Verify branch state**

Run: `git status --short --branch`

Expected: branch is `fork/production-fixes` and tracks `origin/fork/production-fixes`.

---

### Task 2: Add Neutral Fork Maintenance Documentation

**Files:**
- Create: `docs/fork-maintenance.md`
- Optionally modify: `README.md:140-161`

**Step 1: Create the fork maintenance doc**

Add `docs/fork-maintenance.md` with this structure:

```markdown
# Fork Maintenance

This fork keeps a maintained branch for downstream deployments while continuing to pull upstream Hermes changes into `main`.

## Branch Roles

- `main`: follows upstream Hermes as closely as possible.
- `fork/production-fixes`: maintained branch used by downstream deployments; contains `main` plus local fixes.
- `fix/<topic>`: short-lived branch for one local fix, created from `fork/production-fixes`.

## Sync Upstream

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

1. Checkout `fork/production-fixes`.
2. Create a short-lived branch, for example `fix/api-server-model-routing`.
3. Implement the fix.
4. Add or update tests.
5. Run targeted tests.
6. Merge the branch back into `fork/production-fixes`.
7. Verify the maintained branch.
8. Push `fork/production-fixes`.

## Deployment Rule

Downstream deployments should pin to `fork/production-fixes`, not `main`.

If a downstream project pins this repository by commit hash, update it to the final verified `fork/production-fixes` commit hash after the maintenance work is complete.

## Safety Notes

- Do not develop new local fixes directly on `main`.
- Do not deploy directly from `main` if local fixes are required.
- Keep old remote branch names temporarily when renaming deployment branches.
- Resolve upstream merge conflicts on `fork/production-fixes`, then test the affected areas.
```

**Step 2: Link from README if appropriate**

If this fork's `README.md` should expose maintainer docs, add one sentence near `README.md:140`:

```markdown
Fork maintainers should also see [Fork Maintenance](docs/fork-maintenance.md) for the neutral upstream-sync and local-fix workflow.
```

Skip this link if the doc should remain internal and discoverable only by path.

**Step 3: Verify Markdown content**

Run: `git diff -- docs/fork-maintenance.md README.md`

Expected: only the neutral fork-maintenance documentation changed.

**Step 4: Commit docs if requested**

Only commit if the user explicitly asks.

Suggested commit message: `docs: document fork maintenance workflow`

---

### Task 3: Add Unit Tests For Upstream Sync Helper

**Files:**
- Modify: `tests/hermes_cli/test_update_autostash.py`
- Read-only reference: `hermes_cli/main.py:5846-5958`

**Step 1: Add a subprocess fake for upstream sync**

Append tests near the existing update tests in `tests/hermes_cli/test_update_autostash.py`.

Use a helper shaped like:

```python
def _make_upstream_sync_run(*, origin_ahead="0", upstream_ahead="0", fetch_fails=False, pull_fails=False, push_ok=True):
    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append((cmd, kwargs))
        if cmd == ["git", "remote"]:
            return SimpleNamespace(stdout="origin\nupstream\n", stderr="", returncode=0)
        if cmd == ["git", "fetch", "upstream", "--quiet"]:
            if fetch_fails:
                raise CalledProcessError(returncode=128, cmd=cmd)
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if cmd == ["git", "rev-list", "--count", "upstream/main..origin/main"]:
            return SimpleNamespace(stdout=f"{origin_ahead}\n", stderr="", returncode=0)
        if cmd == ["git", "rev-list", "--count", "origin/main..upstream/main"]:
            return SimpleNamespace(stdout=f"{upstream_ahead}\n", stderr="", returncode=0)
        if cmd == ["git", "pull", "--ff-only", "upstream", "main"]:
            if pull_fails:
                raise CalledProcessError(returncode=128, cmd=cmd)
            return SimpleNamespace(stdout="Updating\n", stderr="", returncode=0)
        if cmd == ["git", "push", "origin", "main"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0 if push_ok else 1)
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run, recorded
```

Adjust exact expected commands if `_sync_fork_with_upstream` uses a different push command.

**Step 2: Test fork-local commits skip upstream sync**

Add:

```python
def test_sync_with_upstream_skips_when_origin_has_local_commits(monkeypatch, tmp_path, capsys):
    fake_run, recorded = _make_upstream_sync_run(origin_ahead="2", upstream_ahead="3")
    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    hermes_main._sync_with_upstream_if_needed(["git"], tmp_path)

    commands = [cmd for cmd, _ in recorded]
    assert ["git", "pull", "--ff-only", "upstream", "main"] not in commands
    out = capsys.readouterr().out
    assert "not on upstream" in out
    assert "git pull upstream main" in out
```

**Step 3: Run the new single test and verify failure if helper is wrong**

Run: `scripts/run_tests.sh tests/hermes_cli/test_update_autostash.py::test_sync_with_upstream_skips_when_origin_has_local_commits -v`

Expected: PASS if the fake matches existing code; otherwise adjust the fake to match actual helper commands, not production code.

**Step 4: Test no-op when upstream is current**

Add:

```python
def test_sync_with_upstream_noops_when_up_to_date(monkeypatch, tmp_path, capsys):
    fake_run, recorded = _make_upstream_sync_run(origin_ahead="0", upstream_ahead="0")
    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    hermes_main._sync_with_upstream_if_needed(["git"], tmp_path)

    commands = [cmd for cmd, _ in recorded]
    assert ["git", "pull", "--ff-only", "upstream", "main"] not in commands
    assert "Fork is up to date with upstream" in capsys.readouterr().out
```

**Step 5: Test fast-forward path**

Add:

```python
def test_sync_with_upstream_pulls_and_pushes_when_upstream_is_ahead(monkeypatch, tmp_path, capsys):
    fake_run, recorded = _make_upstream_sync_run(origin_ahead="0", upstream_ahead="4")
    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    hermes_main._sync_with_upstream_if_needed(["git"], tmp_path)

    commands = [cmd for cmd, _ in recorded]
    assert ["git", "pull", "--ff-only", "upstream", "main"] in commands
    assert ["git", "push", "origin", "main"] in commands
    out = capsys.readouterr().out
    assert "Updated from upstream" in out
    assert "Fork synced with upstream" in out
```

**Step 6: Test fetch failure path**

Add:

```python
def test_sync_with_upstream_stops_when_fetch_fails(monkeypatch, tmp_path, capsys):
    fake_run, recorded = _make_upstream_sync_run(fetch_fails=True)
    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    hermes_main._sync_with_upstream_if_needed(["git"], tmp_path)

    commands = [cmd for cmd, _ in recorded]
    assert ["git", "rev-list", "--count", "upstream/main..origin/main"] not in commands
    assert "Failed to fetch upstream" in capsys.readouterr().out
```

**Step 7: Test missing upstream declined path**

Add:

```python
def test_sync_with_upstream_can_decline_adding_missing_remote(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote"]:
            return SimpleNamespace(stdout="origin\n", stderr="", returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)
    monkeypatch.setattr(hermes_main, "_should_skip_upstream_prompt", lambda: False)
    monkeypatch.setattr(hermes_main, "_mark_skip_upstream_prompt", lambda: None)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")

    hermes_main._sync_with_upstream_if_needed(["git"], tmp_path)

    assert calls == [["git", "remote"]]
    out = capsys.readouterr().out
    assert "not tracking the official Hermes repository" in out
    assert "Skipped" in out
```

**Step 8: Run the upstream-sync test group**

Run: `scripts/run_tests.sh tests/hermes_cli/test_update_autostash.py -k upstream -v`

Expected: all upstream-sync helper tests pass.

**Step 9: Commit tests if requested**

Only commit if the user explicitly asks.

Suggested commit message: `test(update): cover upstream fork sync`

---

### Task 4: Verify The Whole Update Area

**Files:**
- No edits

**Step 1: Run targeted update tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_update_autostash.py tests/hermes_cli/test_cmd_update.py -v`

Expected: all selected tests pass.

**Step 2: Check docs diff**

Run: `git diff -- docs/plans/2026-05-14-neutral-fork-maintenance-design.md docs/plans/2026-05-14-fork-maintenance-implementation.md docs/fork-maintenance.md README.md tests/hermes_cli/test_update_autostash.py`

Expected: diff contains only the approved plan, neutral fork docs, optional README link, and upstream-sync tests.

**Step 3: Run full suite before publishing if time allows**

Run: `scripts/run_tests.sh`

Expected: full suite passes.

---

### Task 5: Update Downstream Project References

**Files:**
- No edits in this repository unless downstream project references live here
- Modify downstream project files that pin this repository by commit hash

**Step 1: Capture the final maintained-branch commit hash**

Run: `git rev-parse fork/production-fixes`

Expected: prints the full commit hash for the verified `fork/production-fixes` branch.

**Step 2: Report the commit hash to the user**

Include the exact hash in the completion message so downstream projects can be updated even if they are not accessible from this repository.

Expected: final response includes a line like `Downstream commit hash: <hash>`.

**Step 3: Update downstream project pins where accessible**

For each downstream project that references this repository by commit hash, replace the old hash with the final hash from Step 1.

Expected: downstream projects reference the verified `fork/production-fixes` commit, not the old branch name or old hash.

**Step 4: Verify downstream references**

Run the relevant downstream project checks if those projects are available in the workspace.

Expected: downstream checks pass, or the final response states which downstream checks could not be run and why.

---

### Task 6: Retire The Old Remote Branch After Consumers Move

**Files:**
- No file edits
- Git remote branch state only

**Step 1: Confirm downstream consumers moved**

Verify every deployment that used `planner5d/api-server-multi-model` now uses `fork/production-fixes`.

Expected: no active deployment still depends on the old branch name.

**Step 2: Delete the old remote branch only after confirmation**

Run: `git push origin --delete planner5d/api-server-multi-model`

Expected: old remote branch is deleted.

**Step 3: Prune stale local remote refs**

Run: `git fetch origin --prune`

Expected: `origin/planner5d/api-server-multi-model` no longer appears in `git branch --all`.
