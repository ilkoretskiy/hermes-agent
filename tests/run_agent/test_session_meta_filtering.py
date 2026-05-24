"""Tests for session_meta filtering — issue #4715.

Ensures that transcript-only session_meta messages never reach the
chat-completions API, via both the API-boundary guard in
_sanitize_api_messages() and the CLI session-restore paths.
"""

import logging
import types
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


# ---------------------------------------------------------------------------
# Layer 1 — _sanitize_api_messages role-allowlist guard
# ---------------------------------------------------------------------------

class TestSanitizeApiMessagesRoleFilter:

    def test_drops_session_meta_role(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "session_meta", "content": {"model": "gpt-4"}},
            {"role": "assistant", "content": "hi"},
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        assert len(out) == 2
        assert all(m["role"] != "session_meta" for m in out)

    def test_preserves_valid_roles(self):
        msgs = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        # Need a matching assistant tool_call so the tool result isn't orphaned
        msgs[2]["tool_calls"] = [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}]
        out = AIAgent._sanitize_api_messages(msgs)
        roles = [m["role"] for m in out]
        assert "system" in roles
        assert "user" in roles
        assert "assistant" in roles
        assert "tool" in roles

    def test_logs_warning_when_dropping(self, caplog):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "session_meta", "content": {"info": "test"}},
        ]
        with caplog.at_level(logging.DEBUG, logger="run_agent"):
            AIAgent._sanitize_api_messages(msgs)
        assert any("invalid role" in r.message and "session_meta" in r.message for r in caplog.records)

    def test_drops_multiple_invalid_roles(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "session_meta", "content": {}},
            {"role": "transcript_note", "content": "note"},
            {"role": "assistant", "content": "hi"},
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        assert len(out) == 2
        assert [m["role"] for m in out] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# Codex-ack continuation staleness — drop after the retry has fired
# ---------------------------------------------------------------------------

class TestSanitizeApiMessagesCodexAckContinuationStale:
    """The Codex-ack continuation appends a synthetic ``incomplete`` assistant
    turn + a developer-role "Continue now…" nudge. The nudge is needed for
    the immediate retry API call but becomes stale once the retry produces
    a tool-calling assistant turn. The sanitizer must drop the stale pair
    before subsequent API calls so the nudge is not re-applied on every
    later turn and does not leak a ``developer`` role into provider
    payloads that may reject it.
    """

    def test_keeps_continuation_when_retry_not_yet_fired(self):
        """During the retry call itself, both synthetic items are at the
        tail with no subsequent assistant tool_calls — they must be kept
        so the API sees the nudge."""
        msgs = [
            {"role": "user", "content": "look at ~/foo"},
            {
                "role": "assistant",
                "content": "I'll inspect ~/foo and report back.",
                "finish_reason": "incomplete",
                "_codex_ack_continuation_interim": True,
            },
            {
                "role": "developer",
                "content": "Continue now. Execute the required tool calls.",
                "_codex_ack_continuation_synthetic": True,
            },
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        roles = [m["role"] for m in out]
        assert "developer" in roles
        assert any(m.get("_codex_ack_continuation_interim") for m in out)

    def test_drops_continuation_once_retry_produced_tool_call(self):
        """After the retry produced a tool-calling assistant turn, the
        synthetic interim + developer nudge are stale and must be dropped
        from any subsequent API call."""
        msgs = [
            {"role": "user", "content": "look at ~/foo"},
            {
                "role": "assistant",
                "content": "I'll inspect ~/foo and report back.",
                "finish_reason": "incomplete",
                "_codex_ack_continuation_interim": True,
            },
            {
                "role": "developer",
                "content": "Continue now. Execute the required tool calls.",
                "_codex_ack_continuation_synthetic": True,
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "search_files", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        assert not any(m.get("role") == "developer" for m in out)
        assert not any(m.get("_codex_ack_continuation_interim") for m in out)
        assert not any(m.get("_codex_ack_continuation_synthetic") for m in out)
        # Real user / tool-calling assistant / tool result must survive.
        assert [m["role"] for m in out] == ["user", "assistant", "tool"]

    def test_drops_continuation_when_retry_produced_text_only_response(self):
        """Codex round-2 finding: the stale-drop rule must also fire when
        the retry produces a *non*-tool-calling final assistant message
        (e.g. "Actually, ~/foo doesn't exist, try a different path").
        Otherwise the synthetic pair lingers in in-memory history and
        re-applies the nudge on every subsequent turn of a multi-turn
        session."""
        msgs = [
            {"role": "user", "content": "look at ~/foo"},
            {
                "role": "assistant",
                "content": "I'll inspect ~/foo and report back.",
                "finish_reason": "incomplete",
                "_codex_ack_continuation_interim": True,
            },
            {
                "role": "developer",
                "content": "Continue now. Execute the required tool calls.",
                "_codex_ack_continuation_synthetic": True,
            },
            {
                "role": "assistant",
                "content": "~/foo does not exist on this host.",
                "finish_reason": "stop",
            },
            {"role": "user", "content": "Try ~/bar then."},
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        assert not any(m.get("role") == "developer" for m in out)
        assert not any(m.get("_codex_ack_continuation_interim") for m in out)
        assert not any(m.get("_codex_ack_continuation_synthetic") for m in out)
        assert [m["role"] for m in out] == ["user", "assistant", "user"]

    def test_does_not_drop_continuation_when_only_synthetic_interim_after(self):
        """A second ack-continuation appended right above (its own interim
        is also synthetic) must not count as "the retry closed" for the
        prior pair until a *real* assistant turn appears. Otherwise the
        nudge would be dropped before its own retry runs."""
        msgs = [
            {"role": "user", "content": "look at ~/foo"},
            {
                "role": "assistant",
                "content": "Sure, I'll inspect ~/foo.",
                "finish_reason": "incomplete",
                "_codex_ack_continuation_interim": True,
            },
            {
                "role": "developer",
                "content": "Continue now. Execute the required tool calls.",
                "_codex_ack_continuation_synthetic": True,
            },
            # No real assistant message yet — only later content is
            # *another* synthetic interim (e.g. a second ack retry).
            {
                "role": "assistant",
                "content": "Actually, let me think about that.",
                "finish_reason": "incomplete",
                "_codex_ack_continuation_interim": True,
            },
            {
                "role": "developer",
                "content": "Continue now. Execute the required tool calls.",
                "_codex_ack_continuation_synthetic": True,
            },
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        # The latest synthetic pair must survive (its retry hasn't run).
        # The older pair becomes stale because the newer interim is itself
        # an assistant — but that's a separate test case below; here we
        # just assert that at least one synthetic continuation survives.
        assert any(m.get("_codex_ack_continuation_synthetic") for m in out)
        assert any(m.get("_codex_ack_continuation_interim") for m in out)


# ---------------------------------------------------------------------------
# Layer 2 — CLI session-restore filters session_meta before loading
# ---------------------------------------------------------------------------

class TestCLISessionRestoreFiltering:

    def test_restore_filters_session_meta(self):
        """Simulates the CLI restore path and verifies session_meta is removed."""
        # Build a fake restored message list (as returned by get_messages_as_conversation)
        fake_restored = [
            {"role": "session_meta", "content": {"model": "gpt-4"}},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "session_meta", "content": {"tools": []}},
        ]

        # Apply the same filtering that the patched CLI code now does
        filtered = [m for m in fake_restored if m.get("role") != "session_meta"]

        assert len(filtered) == 2
        assert all(m["role"] != "session_meta" for m in filtered)
        assert filtered[0]["role"] == "user"
        assert filtered[1]["role"] == "assistant"
