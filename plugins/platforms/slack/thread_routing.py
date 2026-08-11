"""Isolated durable stop/resume policy for Slack thread routing.

The adapter supplies Slack-specific routing facts; this module owns only policy,
pattern validation, and thread-scoped strict-state persistence.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

try:
    from re import _parser as _regex_parser  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - compatibility for pre-3.11 CPython
    try:
        import sre_parse as _regex_parser  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover - configured regexes fail closed
        _regex_parser = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

_MAX_PATTERNS_PER_KIND = 20
_MAX_PATTERN_LENGTH = 256
_MAX_CONTROL_TEXT_LENGTH = 4096
_MAX_STATE_ENTRIES = 5000
_MAX_STATE_FILE_BYTES = 1024 * 1024
_MAX_STATE_FIELD_LENGTH = 256
_TRANSACTION_MARKER_PENDING = b"pending\n"
_TRANSACTION_MARKER_COMMITTED = b"committed\n"
_STATE_RECORD_FIELDS = frozenset({
    "mode",
    "team_id",
    "channel_id",
    "thread_ts",
    "set_at",
    "set_by_user",
    "set_by_message_ts",
    "matched_pattern",
})
_SLACK_USER_ID_PREFIXES = frozenset({"U", "W"})
_SLACK_USER_ID_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_SIMPLE_REPEAT_ATOMS = {"LITERAL", "NOT_LITERAL", "IN", "CATEGORY"}
_REPEAT_OPS = {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}
_MAX_FINITE_REPEAT = 100
_MAX_REPEAT_PATHS = 64
_REPEAT_SPACE = "repeat_space"
_REPEAT_OTHER = "repeat_other"
_REPEAT_BOUNDED = "repeat_bounded"
_NONSPACE_LITERAL = "nonspace_literal"
_START_ANCHOR = "start_anchor"
_START_ANCHORS = {"AT_BEGINNING", "AT_BEGINNING_LINE", "AT_BEGINNING_STRING"}


class RoutingDisposition(str, Enum):
    """The adapter action required by an explicit thread-routing policy result."""

    DEFER = "defer"
    ROUTE = "route"
    SUPPRESS_CONTROL = "suppress_control"
    SUPPRESS_STRICT = "suppress_strict"
    SUPPRESS_OTHER_USER_MENTION = "suppress_other_user_mention"


class _CommitOutcome(Enum):
    NOT_COMMITTED = "not_committed"
    COMMITTED = "committed"
    POSSIBLY_COMMITTED = "possibly_committed"


class _PossiblyCommittedStateError(OSError):
    """Persistence completed far enough that rollback cannot be proven."""


@dataclass(frozen=True)
class RoutingContext:
    """Slack facts required for a single thread-policy evaluation."""

    team_id: str
    channel_id: str
    thread_ts: str
    user_id: str
    message_ts: str
    text: str
    direct_bot_mention: bool
    active_agent_thread: bool = False
    mentions_other_user: bool = False
    is_command: bool = False
    peer_bot: bool = False


@dataclass(frozen=True)
class RoutingDecision:
    disposition: RoutingDisposition
    strict: bool = False
    matched_pattern: Optional[str] = None


@dataclass
class _StateAccess:
    state: dict[str, Any]
    valid: bool
    dirty: bool = False
    commit_outcome: _CommitOutcome = _CommitOutcome.NOT_COMMITTED


_process_locks: dict[Path, threading.RLock] = {}
_process_locks_guard = threading.Lock()


def _as_enabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)


def _config_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(value) for value in raw]
    return []


def _slack_user_mention_tokens(text: str) -> list[tuple[str, int, int]]:
    """Return validated ``(user_id, start, end)`` Slack mention tokens."""
    mentions: list[tuple[str, int, int]] = []
    cursor = 0
    text = text or ""
    while True:
        start = text.find("<@", cursor)
        if start < 0:
            break
        close = -1
        depth = 1
        nested = False
        scan = start + 2
        while scan < len(text):
            if text[scan] == "<":
                depth += 1
                nested = True
            elif text[scan] == ">":
                depth -= 1
                if depth == 0:
                    close = scan
                    break
            scan += 1
        if close < 0:
            break
        cursor = close + 1

        if (
            nested
            or (start > 0 and text[start - 1] == "<")
            or (cursor < len(text) and text[cursor] == ">")
        ):
            continue

        body = text[start + 2 : close]
        user_id = body.partition("|")[0]
        if (
            len(user_id) < 2
            or user_id[0] not in _SLACK_USER_ID_PREFIXES
            or any(char not in _SLACK_USER_ID_CHARACTERS for char in user_id[1:])
        ):
            continue
        mentions.append((user_id, start, cursor))
    return mentions


def slack_user_mention_tokens(text: str) -> list[tuple[str, int, int]]:
    """Return validated Slack user mention IDs and source spans."""
    return _slack_user_mention_tokens(text)


def slack_user_mentions(text: str) -> set[str]:
    """Extract well-formed Slack user IDs without scanning inside bad markup."""
    return {user_id for user_id, _, _ in slack_user_mention_tokens(text)}


def strip_slack_user_mentions(text: str, user_ids: set[str]) -> str:
    """Remove validated Slack mentions for ``user_ids`` from ``text``."""
    if not text or not user_ids:
        return text
    parts: list[str] = []
    cursor = 0
    for user_id, start, end in slack_user_mention_tokens(text):
        if user_id not in user_ids:
            continue
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _valid_set_at(record: Mapping[str, Any]) -> bool:
    if "set_at" not in record:
        return True
    value = record.get("set_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _valid_state_record(key: Any, record: Any) -> bool:
    if not isinstance(key, str) or not isinstance(record, dict):
        return False
    scope = key.split(":", 2)
    if (
        len(scope) != 3
        or not all(scope)
        or any(len(component) > _MAX_STATE_FIELD_LENGTH for component in scope)
        or not set(record).issubset(_STATE_RECORD_FIELDS)
        or record.get("mode") != "strict_mention"
        or not _valid_set_at(record)
    ):
        return False
    expected_scope = dict(zip(("team_id", "channel_id", "thread_ts"), scope))
    for field, value in record.items():
        if field in {"mode", "set_at"}:
            continue
        if not isinstance(value, str) or len(value) > _MAX_STATE_FIELD_LENGTH:
            return False
        if field in expected_scope and value != expected_scope[field]:
            return False
    return True


def _serialize_state(state: Mapping[str, Any]) -> bytes:
    return json.dumps(
        state,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _fixed_assertion(tokens: Any) -> bool:
    for op, argument in tokens:
        name = str(op)
        if name in {"LITERAL", "NOT_LITERAL", "IN", "CATEGORY", "AT"}:
            continue
        if name == "SUBPATTERN" and _fixed_assertion(argument[-1]):
            continue
        return False
    return True


def _uses_multiline_mode(tokens: Any) -> bool:
    for op, argument in tokens:
        name = str(op)
        if name == "SUBPATTERN":
            if argument[1] & re.MULTILINE or _uses_multiline_mode(argument[-1]):
                return True
        elif name == "BRANCH":
            if any(_uses_multiline_mode(branch) for branch in argument[1]):
                return True
        elif name in {"ASSERT", "ASSERT_NOT"}:
            if _uses_multiline_mode(argument[1]):
                return True
        elif name in _REPEAT_OPS and _uses_multiline_mode(argument[-1]):
            return True
    return False


def _safe_pattern_sequence(tokens: Any) -> bool:
    seen_required_literal = False
    previous_unbounded_repeat = False
    for op, argument in tokens:
        name = str(op)
        if name.startswith("GROUPREF"):
            return False
        if name in {"LITERAL", "NOT_LITERAL"}:
            seen_required_literal = True
            previous_unbounded_repeat = False
            continue
        if name in {"IN", "CATEGORY", "ANY", "AT"}:
            previous_unbounded_repeat = False
            continue
        if name == "SUBPATTERN":
            if not _safe_pattern_sequence(argument[-1]):
                return False
            previous_unbounded_repeat = False
            continue
        if name == "BRANCH":
            if not all(_safe_pattern_sequence(branch) for branch in argument[1]):
                return False
            previous_unbounded_repeat = False
            continue
        if name in {"ASSERT", "ASSERT_NOT"}:
            if not _fixed_assertion(argument[1]):
                return False
            previous_unbounded_repeat = False
            continue
        if name in _REPEAT_OPS:
            _, maximum, repeated = argument
            unbounded = maximum == getattr(_regex_parser, "MAXREPEAT", None)
            if unbounded:
                if (
                    not seen_required_literal
                    or previous_unbounded_repeat
                    or len(repeated) != 1
                    or str(repeated[0][0]) not in _SIMPLE_REPEAT_ATOMS
                ):
                    return False
            elif maximum > _MAX_FINITE_REPEAT:
                return False
            elif maximum > 1 and (
                len(repeated) != 1 or str(repeated[0][0]) not in _SIMPLE_REPEAT_ATOMS
            ):
                return False
            elif maximum <= 1 and not _safe_pattern_sequence(repeated):
                return False
            previous_unbounded_repeat = unbounded
            continue
        return False
    return True


def _space_repeat(repeated: Any) -> bool:
    if len(repeated) != 1:
        return False
    op, argument = repeated[0]
    name = str(op)
    if name == "CATEGORY":
        return str(argument) == "CATEGORY_SPACE"
    return (
        name == "IN"
        and len(argument) == 1
        and str(argument[0][0]) == "CATEGORY"
        and str(argument[0][1]) == "CATEGORY_SPACE"
    )


def _combine_repeat_paths(
    prefixes: list[tuple[str, ...]],
    suffixes: list[tuple[str, ...]],
) -> Optional[list[tuple[str, ...]]]:
    if len(prefixes) * len(suffixes) > _MAX_REPEAT_PATHS:
        return None
    return [prefix + suffix for prefix in prefixes for suffix in suffixes]


def _repeat_paths(tokens: Any) -> Optional[list[tuple[str, ...]]]:
    paths: list[tuple[str, ...]] = [()]
    max_repeat = getattr(_regex_parser, "MAXREPEAT", None)
    for op, argument in tokens:
        name = str(op)
        choices: list[tuple[str, ...]] = [()]
        if name == "LITERAL":
            if not chr(argument).isspace():
                choices = [(_NONSPACE_LITERAL,)]
        elif name == "AT" and str(argument) in _START_ANCHORS:
            choices = [(_START_ANCHOR,)]
        elif name == "SUBPATTERN":
            nested = _repeat_paths(argument[-1])
            if nested is None:
                return None
            choices = nested
        elif name == "BRANCH":
            choices = []
            for branch in argument[1]:
                nested = _repeat_paths(branch)
                if nested is None or len(choices) + len(nested) > _MAX_REPEAT_PATHS:
                    return None
                choices.extend(nested)
        elif name in _REPEAT_OPS:
            minimum, maximum, repeated = argument
            if maximum == max_repeat:
                marker = _REPEAT_SPACE if _space_repeat(repeated) else _REPEAT_OTHER
                choices = [(marker,)]
            elif maximum <= 1:
                nested = _repeat_paths(repeated)
                if nested is None:
                    return None
                choices = ([()] if minimum == 0 else []) + nested
            elif minimum != maximum:
                marker = _REPEAT_SPACE if _space_repeat(repeated) else _REPEAT_BOUNDED
                choices = [(marker,)]
        combined = _combine_repeat_paths(paths, choices)
        if combined is None:
            return None
        paths = combined
    return paths


def _variable_repeats_are_linear(tokens: Any) -> bool:
    paths = _repeat_paths(tokens)
    if paths is None:
        return False
    repeat_markers = {_REPEAT_SPACE, _REPEAT_OTHER, _REPEAT_BOUNDED}
    for path in paths:
        positions = [
            index for index, marker in enumerate(path) if marker in repeat_markers
        ]
        if not positions:
            continue
        if len(positions) > 1 and any(
            path[index] != _REPEAT_SPACE for index in positions
        ):
            return False

        previous = -1
        for position in positions:
            marker = path[position]
            if marker == _REPEAT_OTHER:
                if _START_ANCHOR not in path[:position]:
                    return False
            elif marker == _REPEAT_SPACE:
                prefix = path[previous + 1 : position]
                anchored_first = previous < 0 and _START_ANCHOR in prefix
                if not anchored_first and _NONSPACE_LITERAL not in prefix:
                    return False
            previous = position
    return True


def _pattern_is_safe(pattern: str) -> bool:
    if _regex_parser is None:
        return False
    try:
        tokens = _regex_parser.parse(pattern, re.IGNORECASE)
        flags = getattr(getattr(tokens, "state", None), "flags", 0)
        return (
            not flags & re.MULTILINE
            and not _uses_multiline_mode(tokens)
            and _safe_pattern_sequence(tokens)
            and _variable_repeats_are_linear(tokens)
        )
    except (OverflowError, RuntimeError, ValueError, re.error):
        return False


def compile_bounded_patterns(
    raw: Any,
    *,
    kind: str,
) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Compile a bounded set of conservatively safe configured regexes.

    Diagnostics intentionally identify only the pattern class and rejection
    reason. Configured expressions may contain sensitive literals and must not
    be copied into logs or exception text.
    """
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for raw_pattern in _config_list(raw)[:_MAX_PATTERNS_PER_KIND]:
        pattern = raw_pattern.strip()
        if not pattern:
            continue
        if len(pattern) > _MAX_PATTERN_LENGTH:
            logger.warning(
                "[Slack] Ignoring %s pattern longer than %d characters",
                kind,
                _MAX_PATTERN_LENGTH,
            )
            continue
        if not _pattern_is_safe(pattern):
            logger.warning("[Slack] Ignoring unsafe %s pattern", kind)
            continue
        try:
            patterns.append((pattern, re.compile(pattern, re.IGNORECASE)))
        except re.error:
            logger.warning("[Slack] Ignoring invalid %s pattern", kind)
    return tuple(patterns)


def match_bounded_pattern(
    patterns: tuple[tuple[str, re.Pattern[str]], ...],
    text: str,
) -> Optional[str]:
    """Return the first matching source expression over bounded input text."""
    bounded_text = (text or "")[:_MAX_CONTROL_TEXT_LENGTH]
    for raw, pattern in patterns:
        if pattern.search(bounded_text):
            return raw
    return None


class ThreadRoutingPolicy:
    """Evaluate Slack thread controls and maintain durable strict state.

    State keys are always ``team_id:channel_id:thread_ts`` and all mutations
    use a process-local lock plus POSIX advisory file lock. Invalid state or an
    unavailable durable store fails closed so an uncertain state cannot wake the
    agent or be overwritten.
    """

    def __init__(self, config: Mapping[str, Any] | None = None):
        self._config = dict(config or {})
        self.enabled = _as_enabled(self._config.get("enabled", False))
        self._state_path = self._resolve_state_path(self._config.get("state_file"))
        self._stop_patterns = self._compile_patterns("stop")
        self._resume_patterns = self._compile_patterns("resume")
        self._suppress_other_user_mentions = _as_enabled(
            self._config.get("suppress_other_user_mentions", True)
        )
        self._locking_warned = False

    @staticmethod
    def _resolve_state_path(raw_path: Any) -> Path:
        path = Path(str(raw_path or "slack/thread_routing_state.json")).expanduser()
        if path.is_absolute():
            return path
        try:
            from hermes_constants import get_hermes_home

            return get_hermes_home() / path
        except Exception:  # pragma: no cover - defensive bootstrap fallback
            return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / path

    @staticmethod
    def key(team_id: str, channel_id: str, thread_ts: str) -> str:
        return f"{team_id}:{channel_id}:{thread_ts}"

    def _transaction_marker_path(self) -> Path:
        return self._state_path.with_name(f".{self._state_path.name}.pending")

    @staticmethod
    def mentions_other_user(text: str, bot_user_id: Optional[str]) -> bool:
        mentioned = slack_user_mentions(text)
        if bot_user_id:
            mentioned.discard(bot_user_id)
        return bool(mentioned)

    def _compile_patterns(self, kind: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
        return compile_bounded_patterns(
            self._config.get(f"{kind}_patterns"),
            kind=f"thread {kind}",
        )

    @staticmethod
    def _valid_scope(team_id: str, channel_id: str, thread_ts: str) -> bool:
        return all(
            isinstance(value, str) and 0 < len(value) <= _MAX_STATE_FIELD_LENGTH
            for value in (team_id, channel_id, thread_ts)
        )

    def _match(
        self, patterns: tuple[tuple[str, re.Pattern[str]], ...], text: str
    ) -> Optional[str]:
        return match_bounded_pattern(patterns, text)

    def evaluate(self, context: RoutingContext) -> RoutingDecision:
        """Return the policy result after any accepted durable state mutation."""
        if not self.enabled or not self._valid_scope(
            context.team_id, context.channel_id, context.thread_ts
        ):
            return RoutingDecision(RoutingDisposition.DEFER)

        # Commands are classified by the adapter before policy evaluation and
        # retain normal gateway semantics. They never change strict state.
        if context.is_command:
            return RoutingDecision(
                RoutingDisposition.DEFER, strict=self.is_strict_scope(context)
            )

        resume = None
        stop = None
        if not context.peer_bot:
            resume = self._match(self._resume_patterns, context.text)
            stop = self._match(self._stop_patterns, context.text)

        key = self.key(context.team_id, context.channel_id, context.thread_ts)
        decision = RoutingDecision(RoutingDisposition.DEFER, strict=False)
        with self._locked_state(mutate=True) as access:
            if not access.valid:
                decision = RoutingDecision(
                    RoutingDisposition.SUPPRESS_STRICT, strict=True
                )
            else:
                value = access.state.get(key)
                strict = (
                    isinstance(value, dict) and value.get("mode") == "strict_mention"
                )
                if (
                    not context.peer_bot
                    and not context.direct_bot_mention
                    and self._suppress_other_user_mentions
                    and context.active_agent_thread
                    and context.mentions_other_user
                ):
                    decision = RoutingDecision(
                        RoutingDisposition.SUPPRESS_OTHER_USER_MENTION,
                        strict=strict,
                    )
                elif resume and context.direct_bot_mention and strict:
                    access.state.pop(key, None)
                    access.dirty = True
                    decision = RoutingDecision(
                        RoutingDisposition.ROUTE,
                        strict=False,
                        matched_pattern=resume,
                    )
                elif stop and (
                    context.direct_bot_mention or context.active_agent_thread
                ):
                    record = {
                        "mode": "strict_mention",
                        "team_id": context.team_id,
                        "channel_id": context.channel_id,
                        "thread_ts": context.thread_ts,
                        "set_at": time.time(),
                        "set_by_user": context.user_id,
                        "set_by_message_ts": context.message_ts,
                        "matched_pattern": stop,
                    }
                    if _valid_state_record(key, record):
                        access.state[key] = record
                        self._trim_state(access.state, preserve_key=key)
                        access.dirty = True
                        decision = RoutingDecision(
                            RoutingDisposition.SUPPRESS_CONTROL,
                            strict=True,
                            matched_pattern=stop,
                        )
                    else:
                        decision = RoutingDecision(
                            RoutingDisposition.SUPPRESS_STRICT, strict=True
                        )
                # A peer bot can be subject to established strict mode but can
                # never create or clear it. Upstream allow_bots remains
                # responsible for every non-strict peer-bot decision.
                elif strict and not context.direct_bot_mention:
                    decision = RoutingDecision(
                        RoutingDisposition.SUPPRESS_STRICT, strict=True
                    )
                elif strict and context.direct_bot_mention:
                    decision = RoutingDecision(RoutingDisposition.ROUTE, strict=True)

        if access.dirty and access.commit_outcome is not _CommitOutcome.COMMITTED:
            return RoutingDecision(RoutingDisposition.SUPPRESS_STRICT, strict=True)
        return decision

    def is_strict(self, team_id: str, channel_id: str, thread_ts: str) -> bool:
        if not self.enabled or not self._valid_scope(team_id, channel_id, thread_ts):
            return False
        return self._is_strict_key(self.key(team_id, channel_id, thread_ts))

    def is_strict_scope(self, context: RoutingContext) -> bool:
        return self.is_strict(context.team_id, context.channel_id, context.thread_ts)

    def _is_strict_key(self, key: str) -> bool:
        strict, _ = self._strict_key_status(key)
        return strict

    def _strict_key_status(self, key: str) -> tuple[bool, bool]:
        with self._locked_state(mutate=False) as access:
            if not access.valid:
                return True, False
            value = access.state.get(key)
            return isinstance(value, dict) and value.get(
                "mode"
            ) == "strict_mention", True

    @staticmethod
    def _trim_state(
        state: dict[str, Any], *, preserve_key: Optional[str] = None
    ) -> None:
        ordered = sorted(
            (item for item in state.items() if item[0] != preserve_key),
            key=lambda item: (
                float(item[1].get("set_at", 0.0)) if isinstance(item[1], dict) else 0.0,
                item[0],
            ),
        )
        entry_overflow = max(0, len(state) - _MAX_STATE_ENTRIES)
        for key, _ in ordered[:entry_overflow]:
            state.pop(key, None)
        if len(_serialize_state(state)) <= _MAX_STATE_FILE_BYTES:
            return
        for key, _ in ordered[entry_overflow:]:
            state.pop(key, None)
            if len(_serialize_state(state)) <= _MAX_STATE_FILE_BYTES:
                return

    @contextmanager
    def _locked_state(self, *, mutate: bool) -> Iterator[_StateAccess]:
        lock = self._process_lock()
        with lock:
            lock_file = self._state_path.with_name(f".{self._state_path.name}.lock")
            try:
                import fcntl
            except (
                ImportError
            ):  # pragma: no cover - Windows is intentionally fail-closed
                self._warn_locking_unavailable()
                yield _StateAccess({}, valid=False)
                return

            handle = None
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                handle = lock_file.open("a+", encoding="utf-8")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                if handle is not None:
                    handle.close()
                self._warn_locking_unavailable(exc)
                yield _StateAccess({}, valid=False)
                return

            try:
                state, valid = self._read_state()
                access = _StateAccess(state, valid=valid)
                yield access
                if mutate and access.valid and access.dirty:
                    try:
                        self._write_state(access.state)
                        access.commit_outcome = _CommitOutcome.COMMITTED
                    except _PossiblyCommittedStateError:
                        logger.warning(
                            "[Slack] Thread routing state mutation may have committed"
                        )
                        access.commit_outcome = _CommitOutcome.POSSIBLY_COMMITTED
                    except OSError as exc:
                        self._warn_locking_unavailable(exc)
            finally:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()

    def _process_lock(self) -> threading.RLock:
        path = self._state_path.resolve()
        with _process_locks_guard:
            return _process_locks.setdefault(path, threading.RLock())

    def _warn_locking_unavailable(self, exc: Optional[Exception] = None) -> None:
        if not self._locking_warned:
            logger.warning(
                "[Slack] Thread routing state is unavailable; failing closed"
            )
            self._locking_warned = True

    def _read_state(self) -> tuple[dict[str, Any], bool]:
        marker_path = self._transaction_marker_path()
        try:
            with marker_path.open("rb") as marker:
                marker_status = marker.read(len(_TRANSACTION_MARKER_COMMITTED) + 1)
        except FileNotFoundError:
            marker_status = _TRANSACTION_MARKER_COMMITTED
        except OSError:
            marker_status = b""
        if marker_status != _TRANSACTION_MARKER_COMMITTED:
            logger.warning(
                "[Slack] Thread routing state has an incomplete durable transaction"
            )
            return {}, False
        if not self._state_path.exists():
            return {}, True
        try:
            with self._state_path.open("rb") as handle:
                serialized = handle.read(_MAX_STATE_FILE_BYTES + 1)
            if len(serialized) > _MAX_STATE_FILE_BYTES:
                logger.warning(
                    "[Slack] Ignoring oversized thread routing state: file exceeds %d bytes",
                    _MAX_STATE_FILE_BYTES,
                )
                return {}, False
            state = json.loads(serialized.decode("utf-8"))
        except Exception:
            logger.warning("[Slack] Ignoring corrupt thread routing state")
            return {}, False
        if not isinstance(state, dict):
            logger.warning(
                "[Slack] Ignoring invalid thread routing state: expected object"
            )
            return {}, False
        if len(state) > _MAX_STATE_ENTRIES:
            logger.warning(
                "[Slack] Ignoring oversized thread routing state: %d entries exceeds %d",
                len(state),
                _MAX_STATE_ENTRIES,
            )
            return {}, False
        for key, value in state.items():
            if not _valid_state_record(key, value):
                logger.warning("[Slack] Ignoring invalid thread routing record")
                return {}, False
        return state, True

    def _fsync_state_directory(self) -> None:
        directory_fd = os.open(self._state_path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _write_transaction_marker(self, status: bytes) -> None:
        marker_path = self._transaction_marker_path()
        marker_created = False
        try:
            marker = marker_path.open("r+b")
        except FileNotFoundError:
            marker = marker_path.open("xb")
            marker_created = True
        with marker:
            try:
                marker.seek(0)
                marker.write(status)
                marker.truncate()
                marker.flush()
                os.fsync(marker.fileno())
            except OSError as commit_error:
                if status == _TRANSACTION_MARKER_COMMITTED:
                    try:
                        marker.seek(0)
                        marker.write(_TRANSACTION_MARKER_PENDING)
                        marker.truncate()
                        marker.flush()
                        os.fsync(marker.fileno())
                    except OSError:
                        logger.warning(
                            "[Slack] Thread routing transaction marker rollback failed"
                        )
                        raise _PossiblyCommittedStateError(
                            "thread routing state mutation may have committed"
                        ) from commit_error
                raise
        if marker_created:
            self._fsync_state_directory()

    def _write_state(self, state: dict[str, Any]) -> None:
        payload = _serialize_state(state)
        if len(payload) > _MAX_STATE_FILE_BYTES:
            raise OSError("thread routing state exceeds the configured byte limit")
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self._state_path.name}.",
            suffix=".tmp",
            dir=self._state_path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._write_transaction_marker(_TRANSACTION_MARKER_PENDING)
            os.replace(temporary_path, self._state_path)
            self._fsync_state_directory()
            self._write_transaction_marker(_TRANSACTION_MARKER_COMMITTED)
        finally:
            if fd >= 0:
                os.close(fd)
            temporary_path.unlink(missing_ok=True)
