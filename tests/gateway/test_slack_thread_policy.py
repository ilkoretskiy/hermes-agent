"""Focused contracts for Slack per-thread stop/resume policy."""

import json
import multiprocessing
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import plugins.platforms.slack.thread_routing as thread_routing
from plugins.platforms.slack.thread_routing import (
    RoutingContext,
    RoutingDisposition,
    ThreadRoutingPolicy,
    compile_bounded_patterns,
    match_bounded_pattern,
)


def _context(text: str, **overrides) -> RoutingContext:
    values = {
        "team_id": "T1",
        "channel_id": "C1",
        "thread_ts": "1710000000.000001",
        "user_id": "U1",
        "message_ts": "1710000001.000002",
        "text": text,
        "direct_bot_mention": False,
    }
    values.update(overrides)
    return RoutingContext(**values)


def _policy(tmp_path, **config) -> ThreadRoutingPolicy:
    return ThreadRoutingPolicy({
        "enabled": True,
        "state_file": str(tmp_path / "thread-routing.json"),
        "stop_patterns": [r"\bstop responding\b"],
        "resume_patterns": [r"\bunmute\b"],
        **config,
    })


def _stop_in_process(state_file: str, thread_ts: str, start_event) -> None:
    policy = ThreadRoutingPolicy({
        "enabled": True,
        "state_file": state_file,
        "stop_patterns": [r"\bstop responding\b"],
    })
    start_event.wait(timeout=5)
    policy.evaluate(
        _context(
            "stop responding",
            thread_ts=thread_ts,
            direct_bot_mention=True,
        )
    )


def test_direct_stop_enters_scoped_strict_mode_and_suppresses_control(tmp_path):
    policy = _policy(tmp_path)

    decision = policy.evaluate(
        _context("<@U_BOT> stop responding", direct_bot_mention=True)
    )

    assert decision.disposition is RoutingDisposition.SUPPRESS_CONTROL
    assert policy.is_strict("T1", "C1", "1710000000.000001") is True


def test_direct_resume_clears_strict_and_routes_once(tmp_path):
    policy = _policy(tmp_path)
    policy.evaluate(_context("stop responding", direct_bot_mention=True))

    decision = policy.evaluate(_context("<@U_BOT> unmute", direct_bot_mention=True))

    assert decision.disposition is RoutingDisposition.ROUTE
    assert policy.is_strict("T1", "C1", "1710000000.000001") is False


def test_unscoped_or_inactive_stop_defers_without_writing_state(tmp_path):
    policy = _policy(tmp_path)

    decision = policy.evaluate(_context("stop responding"))

    assert decision.disposition is RoutingDisposition.DEFER
    assert not (tmp_path / "thread-routing.json").exists()


def test_oversized_scope_defers_without_writing_state(tmp_path):
    policy = _policy(tmp_path)

    decision = policy.evaluate(
        _context(
            "stop responding",
            team_id="T" * 257,
            direct_bot_mention=True,
        )
    )

    assert decision.disposition is RoutingDisposition.DEFER
    assert not (tmp_path / "thread-routing.json").exists()


def test_oversized_audit_metadata_fails_closed_without_writing_state(tmp_path):
    policy = _policy(tmp_path)

    decision = policy.evaluate(
        _context(
            "stop responding",
            user_id="U" * 257,
            direct_bot_mention=True,
        )
    )

    assert decision.disposition is RoutingDisposition.SUPPRESS_STRICT
    assert not (tmp_path / "thread-routing.json").exists()


def test_disabled_policy_defers_even_when_legacy_strict_state_exists(tmp_path):
    state_path = tmp_path / "thread-routing.json"
    state_path.write_text(
        json.dumps({"T1:C1:1710000000.000001": {"mode": "strict_mention"}}),
        encoding="utf-8",
    )
    policy = _policy(tmp_path, enabled=False)

    assert (
        policy.evaluate(_context("ordinary turn")).disposition
        is RoutingDisposition.DEFER
    )


def test_commands_and_peer_bots_never_mutate_strict_state(tmp_path):
    policy = _policy(tmp_path)

    command = policy.evaluate(
        _context("stop responding", direct_bot_mention=True, is_command=True)
    )
    peer_bot = policy.evaluate(
        _context("stop responding", direct_bot_mention=True, peer_bot=True)
    )

    assert command.disposition is RoutingDisposition.DEFER
    assert peer_bot.disposition is RoutingDisposition.DEFER
    assert policy.is_strict("T1", "C1", "1710000000.000001") is False


def test_other_user_mention_parser_supports_bare_and_pipe_forms():
    bot_user_id = "U_BOT"

    assert ThreadRoutingPolicy.mentions_other_user("<@U_OTHER> check", bot_user_id)
    assert ThreadRoutingPolicy.mentions_other_user(
        "<@U_OTHER|alice> check", bot_user_id
    )
    assert not ThreadRoutingPolicy.mentions_other_user(
        "<@U_BOT|hermes> check", bot_user_id
    )
    assert not ThreadRoutingPolicy.mentions_other_user(
        "<#C_OTHER|general> check", bot_user_id
    )
    assert not ThreadRoutingPolicy.mentions_other_user(
        "<@U_OTHER|alice check", bot_user_id
    )


def test_other_user_mention_parser_rejects_non_user_id_prefixes():
    bot_user_id = "U_BOT"

    assert ThreadRoutingPolicy.mentions_other_user(
        "<@W_OTHER|alice> check", bot_user_id
    )
    assert not ThreadRoutingPolicy.mentions_other_user(
        "<@C_OTHER|general> check", bot_user_id
    )
    assert not ThreadRoutingPolicy.mentions_other_user(
        "<@D_OTHER|dm> check", bot_user_id
    )


def test_other_user_mention_parser_bounds_unterminated_pipe_labels():
    text = "<@U_OTHER|" * 4096
    started = time.monotonic()

    assert not ThreadRoutingPolicy.mentions_other_user(text, "U_BOT")
    assert time.monotonic() - started < 0.2


def test_other_user_mention_parser_rejects_nested_and_wrapped_markup():
    bot_user_id = "U_BOT"

    for text in (
        "<@U_OTHER|<@U_NESTED>>",
        "<@U_OTHER|alice <@U_NESTED> tail",
        "<<@U_OTHER|alice>>",
        "<@U_OTHER|alice>>",
    ):
        assert not ThreadRoutingPolicy.mentions_other_user(text, bot_user_id)


def test_user_mention_parser_skips_entire_malformed_outer_token():
    text = "<@U_OTHER|prefix<@U_NOPE><@U_BOT>suffix> <@W_VALID|outside>"

    assert thread_routing.slack_user_mentions(text) == {"W_VALID"}


def test_state_is_scope_isolated_and_legacy_json_is_readable(tmp_path):
    state_path = tmp_path / "thread-routing.json"
    state_path.write_text(
        json.dumps({
            "T1:C1:1710000000.000001": {"mode": "strict_mention"},
        }),
        encoding="utf-8",
    )
    policy = _policy(tmp_path)

    assert policy.is_strict("T1", "C1", "1710000000.000001") is True
    assert policy.is_strict("T2", "C1", "1710000000.000001") is False
    assert policy.is_strict("T1", "C2", "1710000000.000001") is False


def test_state_is_bounded_and_evicts_oldest_record_deterministically(tmp_path):
    state_path = tmp_path / "thread-routing.json"
    state_path.write_text(
        json.dumps({
            f"T1:C1:{index}": {
                "mode": "strict_mention",
                "set_at": float(index),
            }
            for index in range(5000)
        }),
        encoding="utf-8",
    )
    policy = _policy(tmp_path)

    policy.evaluate(
        _context(
            "stop responding",
            thread_ts="new-thread",
            direct_bot_mention=True,
        )
    )

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(persisted) == 5000
    assert "T1:C1:0" not in persisted
    assert "T1:C1:new-thread" in persisted


def test_successful_write_stays_within_state_file_byte_limit(tmp_path):
    state_path = tmp_path / "thread-routing.json"
    state_path.write_text(
        json.dumps({
            f"T1:C1:{index}": {
                "mode": "strict_mention",
                "set_at": float(index),
                "matched_pattern": "x" * 256,
            }
            for index in range(2950)
        }),
        encoding="utf-8",
    )
    policy = _policy(tmp_path)

    decision = policy.evaluate(
        _context(
            "stop responding",
            thread_ts="new-thread",
            direct_bot_mention=True,
        )
    )

    assert decision.disposition is RoutingDisposition.SUPPRESS_CONTROL
    assert state_path.stat().st_size <= thread_routing._MAX_STATE_FILE_BYTES
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert "T1:C1:new-thread" in persisted


def test_oversized_state_fails_closed_without_replacing_state(tmp_path):
    state_path = tmp_path / "thread-routing.json"
    oversized_state = {
        f"T1:C1:{index}": {
            "mode": "strict_mention",
            "set_at": float(index),
        }
        for index in range(5001)
    }
    serialized = json.dumps(oversized_state)
    state_path.write_text(serialized, encoding="utf-8")
    policy = _policy(tmp_path)

    decision = policy.evaluate(_context("ordinary message"))

    assert decision.disposition is RoutingDisposition.SUPPRESS_STRICT
    assert state_path.read_text(encoding="utf-8") == serialized


def test_oversized_state_bytes_for_other_scope_fail_closed_without_replacing_state(
    tmp_path,
):
    state_path = tmp_path / "thread-routing.json"
    serialized = json.dumps({
        "T1:C1:other-thread": {
            "mode": "strict_mention",
            "set_at": 1.0,
            "padding": "x" * 2_000_000,
        }
    })
    state_path.write_text(serialized, encoding="utf-8")
    policy = _policy(tmp_path)

    decision = policy.evaluate(_context("ordinary message"))

    assert decision.disposition is RoutingDisposition.SUPPRESS_STRICT
    assert state_path.read_text(encoding="utf-8") == serialized


def test_corrupt_state_fails_closed(tmp_path):
    state_path = tmp_path / "thread-routing.json"
    state_path.write_text("{invalid", encoding="utf-8")
    policy = _policy(tmp_path)

    decision = policy.evaluate(_context("ordinary message"))

    assert decision.disposition is RoutingDisposition.SUPPRESS_STRICT


def test_state_diagnostics_do_not_expose_configured_path(tmp_path, caplog):
    secret_path_fragment = "SYNTHETIC-SECRET-STATE-PATH"
    state_path = tmp_path / secret_path_fragment / "thread-routing.json"
    state_path.parent.mkdir()
    state_path.write_text("{invalid", encoding="utf-8")
    policy = ThreadRoutingPolicy({
        "enabled": True,
        "state_file": str(state_path),
        "stop_patterns": [r"\bstop\s+responding\b"],
    })

    decision = policy.evaluate(_context("ordinary message"))

    assert decision.disposition is RoutingDisposition.SUPPRESS_STRICT
    assert secret_path_fragment not in caplog.text


def test_invalid_record_fails_closed_without_replacing_state(tmp_path):
    state_path = tmp_path / "thread-routing.json"
    invalid_state = {"T1:C1:1710000000.000001": {"mode": "unexpected"}}
    state_path.write_text(json.dumps(invalid_state), encoding="utf-8")
    policy = _policy(tmp_path)

    decision = policy.evaluate(_context("stop responding", direct_bot_mention=True))

    assert decision.disposition is RoutingDisposition.SUPPRESS_STRICT
    assert json.loads(state_path.read_text(encoding="utf-8")) == invalid_state


def test_unknown_record_field_fails_closed_without_replacing_state(tmp_path):
    state_path = tmp_path / "thread-routing.json"
    invalid_state = {
        "T1:C1:other-thread": {
            "mode": "strict_mention",
            "set_at": 1.0,
            "unexpected": "value",
        }
    }
    state_path.write_text(json.dumps(invalid_state), encoding="utf-8")
    policy = _policy(tmp_path)

    decision = policy.evaluate(_context("ordinary message"))

    assert decision.disposition is RoutingDisposition.SUPPRESS_STRICT
    assert json.loads(state_path.read_text(encoding="utf-8")) == invalid_state


def test_oversized_or_mismatched_record_scope_fails_closed(tmp_path):
    state_path = tmp_path / "thread-routing.json"
    invalid_states = (
        {
            "T1:C1:other-thread": {
                "mode": "strict_mention",
                "set_by_user": "U" * 257,
            }
        },
        {
            "T1:C1:other-thread": {
                "mode": "strict_mention",
                "team_id": "T2",
            }
        },
        {
            f"T1:C1:{'x' * 257}": {
                "mode": "strict_mention",
            }
        },
    )

    for invalid_state in invalid_states:
        serialized = json.dumps(invalid_state)
        state_path.write_text(serialized, encoding="utf-8")
        policy = _policy(tmp_path)

        decision = policy.evaluate(_context("ordinary message"))

        assert decision.disposition is RoutingDisposition.SUPPRESS_STRICT
        assert state_path.read_text(encoding="utf-8") == serialized


def test_invalid_set_at_fails_closed_without_replacing_state(tmp_path):
    state_path = tmp_path / "thread-routing.json"

    for invalid_set_at in ("not-a-number", float("nan"), 10**1000):
        invalid_state = {
            "T1:C1:other-thread": {
                "mode": "strict_mention",
                "set_at": invalid_set_at,
            }
        }
        serialized = json.dumps(invalid_state)
        state_path.write_text(serialized, encoding="utf-8")
        policy = _policy(tmp_path)

        decision = policy.evaluate(_context("ordinary message"))

        assert decision.disposition is RoutingDisposition.SUPPRESS_STRICT
        assert state_path.read_text(encoding="utf-8") == serialized


def test_failed_state_write_does_not_report_successful_stop(tmp_path, monkeypatch):
    policy = _policy(tmp_path)

    def fail_write(state):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(policy, "_write_state", fail_write)

    decision = policy.evaluate(_context("stop responding", direct_bot_mention=True))

    assert decision.disposition is RoutingDisposition.SUPPRESS_STRICT
    assert not (tmp_path / "thread-routing.json").exists()


def test_directory_sync_unavailable_does_not_report_successful_stop(
    tmp_path, monkeypatch
):
    policy = _policy(tmp_path)
    real_open = thread_routing.os.open

    def fail_directory_open(path, flags, *args, **kwargs):
        if path == tmp_path and flags & thread_routing.os.O_DIRECTORY:
            raise OSError("synthetic directory open failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(thread_routing.os, "open", fail_directory_open)

    decision = policy.evaluate(_context("stop responding", direct_bot_mention=True))

    assert decision.disposition is RoutingDisposition.SUPPRESS_STRICT


def test_marker_directory_fsync_failure_does_not_report_successful_stop(
    tmp_path, monkeypatch
):
    policy = _policy(tmp_path)
    real_fsync = thread_routing.os.fsync
    directory_fsync_attempts = 0

    def fail_directory_fsync(fd):
        nonlocal directory_fsync_attempts
        if stat.S_ISDIR(thread_routing.os.fstat(fd).st_mode):
            directory_fsync_attempts += 1
            raise OSError("synthetic directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(thread_routing.os, "fsync", fail_directory_fsync)

    decision = policy.evaluate(_context("stop responding", direct_bot_mention=True))
    later = _policy(tmp_path).evaluate(_context("ordinary message"))

    assert directory_fsync_attempts == 1
    assert decision.disposition is RoutingDisposition.SUPPRESS_STRICT
    assert later.disposition is RoutingDisposition.SUPPRESS_STRICT


def test_failed_resume_directory_fsync_remains_fail_closed_after_restart(
    tmp_path, monkeypatch
):
    policy = _policy(tmp_path)
    policy.evaluate(_context("stop responding", direct_bot_mention=True))
    real_fsync = thread_routing.os.fsync
    real_replace = thread_routing.os.replace
    state_replaced = False

    def track_replace(source, destination):
        nonlocal state_replaced
        result = real_replace(source, destination)
        state_replaced = True
        return result

    def fail_post_replace_directory_fsync(fd):
        if state_replaced and stat.S_ISDIR(thread_routing.os.fstat(fd).st_mode):
            raise OSError("synthetic post-replace directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(thread_routing.os, "replace", track_replace)
    monkeypatch.setattr(thread_routing.os, "fsync", fail_post_replace_directory_fsync)

    resume = policy.evaluate(_context("unmute", direct_bot_mention=True))
    later = _policy(tmp_path).evaluate(_context("ordinary message"))

    assert resume.disposition is RoutingDisposition.SUPPRESS_STRICT
    assert later.disposition is RoutingDisposition.SUPPRESS_STRICT


def test_resume_reads_and_clears_in_one_locked_transaction(tmp_path, monkeypatch):
    policy = _policy(tmp_path)
    policy.evaluate(_context("stop responding", direct_bot_mention=True))
    real_locked_state = policy._locked_state
    transactions = 0

    def track_locked_state(*, mutate):
        nonlocal transactions
        transactions += 1
        return real_locked_state(mutate=mutate)

    monkeypatch.setattr(policy, "_locked_state", track_locked_state)

    decision = policy.evaluate(_context("unmute", direct_bot_mention=True))

    assert decision.disposition is RoutingDisposition.ROUTE
    assert transactions == 1


def test_later_stop_survives_concurrent_resume(tmp_path, monkeypatch):
    policy = _policy(tmp_path)
    policy.evaluate(_context("stop responding", direct_bot_mention=True))
    real_read_state = policy._read_state
    resume_read = threading.Event()
    release_resume = threading.Event()

    class PausingState(dict):
        def get(self, key, default=None):
            if not resume_read.is_set():
                resume_read.set()
                assert release_resume.wait(timeout=2)
            return super().get(key, default)

    def pausing_read_state():
        state, valid = real_read_state()
        return PausingState(state), valid

    monkeypatch.setattr(policy, "_read_state", pausing_read_state)

    with ThreadPoolExecutor(max_workers=2) as executor:
        resume_future = executor.submit(
            policy.evaluate,
            _context("unmute", direct_bot_mention=True),
        )
        assert resume_read.wait(timeout=2)
        stop_future = executor.submit(
            policy.evaluate,
            _context("stop responding", direct_bot_mention=True),
        )
        time.sleep(0.05)
        assert not stop_future.done()
        release_resume.set()
        resume = resume_future.result(timeout=2)
        stop = stop_future.result(timeout=2)

    assert resume.disposition is RoutingDisposition.ROUTE
    assert stop.disposition is RoutingDisposition.SUPPRESS_CONTROL
    assert policy.is_strict_scope(_context("ordinary message"))


def test_successful_transaction_keeps_committed_marker_for_restart(tmp_path):
    policy = _policy(tmp_path)

    stop = policy.evaluate(_context("stop responding", direct_bot_mention=True))
    marker_path = policy._transaction_marker_path()
    later = _policy(tmp_path).evaluate(_context("ordinary message"))

    assert stop.disposition is RoutingDisposition.SUPPRESS_CONTROL
    assert marker_path.read_bytes() == thread_routing._TRANSACTION_MARKER_COMMITTED
    assert later.disposition is RoutingDisposition.SUPPRESS_STRICT


@pytest.mark.parametrize("rollback_fsync_fails", (False, True))
def test_failed_committed_marker_fsync_remains_fail_closed_after_restart(
    tmp_path, monkeypatch, rollback_fsync_fails
):
    policy = _policy(tmp_path)
    policy.evaluate(_context("stop responding", direct_bot_mention=True))
    real_fsync = thread_routing.os.fsync
    fsync_calls = 0

    def fail_committed_marker_fsync(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 4 or (rollback_fsync_fails and fsync_calls == 5):
            raise OSError("synthetic committed-marker fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(thread_routing.os, "fsync", fail_committed_marker_fsync)

    resume = policy.evaluate(_context("unmute", direct_bot_mention=True))
    marker = policy._transaction_marker_path().read_bytes()
    later = _policy(tmp_path).evaluate(_context("ordinary message"))

    assert fsync_calls >= 5
    assert marker == thread_routing._TRANSACTION_MARKER_PENDING
    assert resume.disposition is RoutingDisposition.SUPPRESS_STRICT
    assert later.disposition is RoutingDisposition.SUPPRESS_STRICT


def test_failed_commit_sync_and_rollback_write_suppresses_current_resume(
    tmp_path, monkeypatch
):
    policy = _policy(tmp_path)
    policy.evaluate(_context("stop responding", direct_bot_mention=True))
    marker_path = policy._transaction_marker_path()
    real_path_open = thread_routing.Path.open
    real_fsync = thread_routing.os.fsync
    marker_opens = 0
    commit_marker_fd = None
    commit_fsync_failed = False

    class FailRollbackWrite:
        def __init__(self, handle):
            self._handle = handle
            self.write_attempts = 0

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def write(self, payload):
            self.write_attempts += 1
            if self.write_attempts == 2:
                raise OSError("synthetic rollback marker write failure")
            return self._handle.write(payload)

        def __getattr__(self, name):
            return getattr(self._handle, name)

    def fail_rollback_marker_write(path, *args, **kwargs):
        nonlocal marker_opens, commit_marker_fd
        handle = real_path_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == marker_path and mode == "r+b":
            marker_opens += 1
            if marker_opens == 2:
                commit_marker_fd = handle.fileno()
                return FailRollbackWrite(handle)
        return handle

    def fail_commit_marker_fsync(fd):
        nonlocal commit_fsync_failed
        if fd == commit_marker_fd and not commit_fsync_failed:
            commit_fsync_failed = True
            raise OSError("synthetic committed-marker fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(thread_routing.Path, "open", fail_rollback_marker_write)
    monkeypatch.setattr(thread_routing.os, "fsync", fail_commit_marker_fsync)

    resume = policy.evaluate(_context("unmute", direct_bot_mention=True))
    marker = marker_path.read_bytes()
    persisted = (tmp_path / "thread-routing.json").read_bytes()
    later = _policy(tmp_path).evaluate(_context("ordinary message"))

    assert marker_opens == 2
    assert commit_fsync_failed
    assert marker == thread_routing._TRANSACTION_MARKER_COMMITTED
    assert persisted == b"{}"
    assert resume.disposition is RoutingDisposition.SUPPRESS_STRICT
    assert later.disposition is RoutingDisposition.DEFER


def test_failed_committed_marker_open_remains_fail_closed_after_restart(
    tmp_path, monkeypatch
):
    policy = _policy(tmp_path)
    policy.evaluate(_context("stop responding", direct_bot_mention=True))
    marker_path = policy._transaction_marker_path()
    real_path_open = thread_routing.Path.open
    marker_writes = 0

    def fail_second_marker_write(path, *args, **kwargs):
        nonlocal marker_writes
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == marker_path and mode == "r+b":
            marker_writes += 1
            if marker_writes == 2:
                raise OSError("synthetic committed-marker open failure")
        return real_path_open(path, *args, **kwargs)

    monkeypatch.setattr(thread_routing.Path, "open", fail_second_marker_write)

    resume = policy.evaluate(_context("unmute", direct_bot_mention=True))
    later = _policy(tmp_path).evaluate(_context("ordinary message"))

    assert marker_writes == 2
    assert resume.disposition is RoutingDisposition.SUPPRESS_STRICT
    assert later.disposition is RoutingDisposition.SUPPRESS_STRICT


def test_backreference_pattern_is_rejected_before_matching(tmp_path):
    policy = _policy(tmp_path, stop_patterns=[r"(stop)\s+\1"])

    decision = policy.evaluate(_context("stop stop", direct_bot_mention=True))

    assert decision.disposition is RoutingDisposition.DEFER


def test_ambiguous_alternation_inside_unbounded_repeat_is_rejected(tmp_path):
    policy = _policy(tmp_path, stop_patterns=[r"(a|aa)+$"])

    decision = policy.evaluate(_context("a" * 32, direct_bot_mention=True))

    assert decision.disposition is RoutingDisposition.DEFER


def test_finite_repeat_over_complex_subpattern_is_rejected(tmp_path):
    for pattern in (r"(a|aa){100}$", r"(a[^b]*){100}$"):
        policy = _policy(tmp_path, stop_patterns=[pattern])

        decision = policy.evaluate(_context("a" * 100, direct_bot_mention=True))

        assert decision.disposition is RoutingDisposition.DEFER


def test_overlapping_variable_repeats_are_rejected():
    for pattern in (
        r"a[a-z]*a[a-z]*z",
        r"a\w*a\w*z",
        r"a[^b]*a[^b]*z",
        r"a\s+ \s+ \s+z",
        r"a[a-z]{0,100}a[a-z]{0,100}a[a-z]{0,100}z",
    ):
        assert compile_bounded_patterns([pattern], kind="probe") == ()


def test_unanchored_single_nonspace_variable_repeat_is_rejected():
    for pattern in (r"a[a-z]*z", r"a\w*z", r"a[^b]*z"):
        assert compile_bounded_patterns([pattern], kind="probe") == ()


def test_multiline_start_anchor_does_not_bypass_restart_safety():
    for pattern in (
        r"(?m)^a[\s\S]*z$",
        r"(?m)^a[^b]*z$",
        r"(?m:^a[\s\S]*z$)",
        r"(?m:^a[^b]*z$)",
    ):
        assert compile_bounded_patterns([pattern], kind="probe") == ()


def test_anchored_and_finite_single_repeats_remain_supported():
    patterns = compile_bounded_patterns(
        [r"^a[a-z]*z$", r"a[a-z]{0,100}z", r"a\s+z"],
        kind="probe",
    )

    assert len(patterns) == 3
    assert match_bounded_pattern(patterns, "alphabetz") is not None
    assert match_bounded_pattern(patterns, "a z") is not None


def test_word_separated_whitespace_repeats_remain_supported():
    patterns = compile_bounded_patterns(
        [
            r"\bdo\s+not\s+(respond|reply)\b",
            r"(?<!\w)можешь\s+(снова\s+|опять\s+)?отвечать(?!\w)",
        ],
        kind="probe",
    )

    assert len(patterns) == 2
    assert match_bounded_pattern(patterns, "do not respond") is not None
    assert match_bounded_pattern(patterns, "можешь опять отвечать") is not None


def test_bounded_mention_patterns_reject_unsafe_inputs_and_redact_logs(caplog):
    secret_pattern = r"token-SYNTHETIC-SECRET-(a+)+$"
    patterns = compile_bounded_patterns(
        [
            secret_pattern,
            "x" * 257,
            *[f"safe-{chr(65 + index)}" for index in range(21)],
        ],
        kind="mention",
    )

    assert len(patterns) == 18
    assert match_bounded_pattern(patterns, "safe-R") == "safe-R"
    assert match_bounded_pattern(patterns, "safe-T") is None
    assert match_bounded_pattern(patterns, "prefix " + "x" * 5000 + " safe-R") is None
    assert "SYNTHETIC-SECRET" not in caplog.text


def test_rejected_pattern_diagnostic_does_not_expose_pattern_text(tmp_path, caplog):
    secret_like_pattern = r"token-SYNTHETIC-SECRET-(a+)+$"

    _policy(tmp_path, stop_patterns=[secret_like_pattern])

    assert "Ignoring unsafe thread stop pattern" in caplog.text
    assert "SYNTHETIC-SECRET" not in caplog.text


def test_unsafe_pattern_is_ignored_and_adversarial_long_input_completes_quickly(
    tmp_path,
):
    policy = _policy(tmp_path, stop_patterns=[r"(a+)+$"])
    started = time.monotonic()

    decision = policy.evaluate(_context("a" * 250_000 + "!", direct_bot_mention=True))

    assert decision.disposition is RoutingDisposition.DEFER
    assert time.monotonic() - started < 1.0


def test_concurrent_local_policy_instances_preserve_both_updates(tmp_path):
    def stop(thread_ts: str) -> None:
        policy = _policy(tmp_path)
        policy.evaluate(
            _context(
                "stop responding",
                thread_ts=thread_ts,
                direct_bot_mention=True,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(stop, ["1710000000.000001", "1710000000.000002"]))

    persisted = json.loads(
        (tmp_path / "thread-routing.json").read_text(encoding="utf-8")
    )
    assert set(persisted) == {
        "T1:C1:1710000000.000001",
        "T1:C1:1710000000.000002",
    }


def test_concurrent_processes_preserve_all_updates(tmp_path):
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    state_path = tmp_path / "thread-routing.json"
    thread_timestamps = [f"1710000000.{index:06d}" for index in range(1, 5)]
    processes = [
        context.Process(
            target=_stop_in_process,
            args=(str(state_path), thread_ts, start_event),
        )
        for thread_ts in thread_timestamps
    ]

    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(persisted) == {f"T1:C1:{thread_ts}" for thread_ts in thread_timestamps}
