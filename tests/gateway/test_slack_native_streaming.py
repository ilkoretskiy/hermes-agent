"""Tests: SlackAdapter native streaming (chat.startStream/appendStream/stopStream).

Behaviour contract:
  * supports_draft_streaming: True when connected, False after a cached
    feature-gate failure or when disconnected.
  * send_draft first frame: chat_startStream with thread_ts + initial text;
    returns the stream ts as message_id.
  * send_draft subsequent frames: chat_appendStream with only the delta;
    trailing cursor glyph stripped before delta computation.
  * identical frame: no API call, success.
  * prefix mismatch: stream sealed, frame fails (consumer falls back to edits).
  * send() finalization: active stream sealed via chat_stopStream with the
    remaining delta instead of chat_postMessage (no duplicate message).
  * send() with unrelated content: stream left open, normal post proceeds.
  * startStream feature-gate error: caches _native_stream_unsupported so
    future supports_draft_streaming() returns False.
  * disconnect(): dangling streams sealed.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from plugins.platforms.slack.adapter import SlackAdapter


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="xoxb-fake", extra=extra or {})
    a = SlackAdapter(config)
    a._app = MagicMock()
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "999.111"})
    client.chat_update = AsyncMock(return_value={"ts": "999.111"})
    client.chat_startStream = AsyncMock(return_value={"ok": True, "ts": "123.456"})
    client.chat_appendStream = AsyncMock(return_value={"ok": True})
    client.chat_stopStream = AsyncMock(return_value={"ok": True})
    a._get_client = MagicMock(return_value=client)
    a.stop_typing = AsyncMock()
    a._running = True
    return a, client


META = {"thread_id": "111.000", "user_id": "U123"}


class TestSupportsDraftStreaming:
    def test_supported_when_connected(self):
        adapter, _ = _make_adapter()
        assert adapter.supports_draft_streaming(chat_type="dm") is True

    def test_unsupported_when_disconnected(self):
        adapter, _ = _make_adapter()
        adapter._app = None
        assert adapter.supports_draft_streaming() is False

    def test_unsupported_after_feature_gate_failure(self):
        adapter, _ = _make_adapter()
        adapter._native_stream_unsupported = True
        assert adapter.supports_draft_streaming() is False


class TestSendDraft:
    @pytest.mark.asyncio
    async def test_first_frame_starts_stream(self):
        adapter, client = _make_adapter()
        result = await adapter.send_draft("D1", 7, "Hello wo", metadata=META)
        assert result.success
        assert result.message_id == "123.456"
        kwargs = client.chat_startStream.await_args.kwargs
        assert kwargs["channel"] == "D1"
        assert kwargs["thread_ts"] == "111.000"
        assert kwargs["markdown_text"] == "Hello wo"
        assert kwargs["recipient_user_id"] == "U123"
        client.chat_appendStream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subsequent_frame_appends_delta_only(self):
        adapter, client = _make_adapter()
        await adapter.send_draft("D1", 7, "Hello wo", metadata=META)
        result = await adapter.send_draft("D1", 7, "Hello world!", metadata=META)
        assert result.success
        kwargs = client.chat_appendStream.await_args.kwargs
        assert kwargs["markdown_text"] == "rld!"
        assert kwargs["ts"] == "123.456"

    @pytest.mark.asyncio
    async def test_cursor_glyph_stripped(self):
        adapter, client = _make_adapter()
        await adapter.send_draft("D1", 7, "Hello \u2589", metadata=META)
        assert client.chat_startStream.await_args.kwargs["markdown_text"] == "Hello"
        await adapter.send_draft("D1", 7, "Hello world \u2589", metadata=META)
        assert client.chat_appendStream.await_args.kwargs["markdown_text"] == " world"

    @pytest.mark.asyncio
    async def test_identical_frame_is_noop(self):
        adapter, client = _make_adapter()
        await adapter.send_draft("D1", 7, "Hello", metadata=META)
        result = await adapter.send_draft("D1", 7, "Hello \u2589", metadata=META)
        assert result.success
        client.chat_appendStream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prefix_mismatch_seals_and_fails(self):
        adapter, client = _make_adapter()
        await adapter.send_draft("D1", 7, "Hello", metadata=META)
        result = await adapter.send_draft("D1", 7, "Rewritten text", metadata=META)
        assert not result.success
        client.chat_stopStream.assert_awaited()
        assert not adapter._active_streams

    @pytest.mark.asyncio
    async def test_no_thread_ts_fails_cleanly(self):
        adapter, client = _make_adapter()
        result = await adapter.send_draft("D1", 7, "Hello", metadata={})
        assert not result.success
        client.chat_startStream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_new_draft_id_seals_prior_stream(self):
        adapter, client = _make_adapter()
        await adapter.send_draft("D1", 7, "Segment one", metadata=META)
        client.chat_startStream.return_value = {"ok": True, "ts": "124.000"}
        result = await adapter.send_draft("D1", 8, "Segment two", metadata=META)
        assert result.success
        client.chat_stopStream.assert_awaited()  # sealed segment one
        assert next(iter(adapter._active_streams.values()))["ts"] == "124.000"


class TestFeatureGateFallback:
    @pytest.mark.asyncio
    async def test_not_allowed_caches_unsupported(self):
        adapter, client = _make_adapter()
        client.chat_startStream = AsyncMock(
            side_effect=Exception("The request to the Slack API failed. (not_allowed)")
        )
        result = await adapter.send_draft("D1", 7, "Hello", metadata=META)
        assert not result.success
        assert adapter._native_stream_unsupported is True
        assert adapter.supports_draft_streaming() is False

    @pytest.mark.asyncio
    async def test_transient_error_does_not_cache(self):
        adapter, client = _make_adapter()
        client.chat_startStream = AsyncMock(side_effect=Exception("timeout"))
        result = await adapter.send_draft("D1", 7, "Hello", metadata=META)
        assert not result.success
        assert adapter._native_stream_unsupported is False


class TestSendFinalization:
    def test_direct_adapter_declares_native_stream_as_the_message(self):
        adapter, _ = _make_adapter()
        assert adapter.draft_stream_is_message is True

    @pytest.mark.asyncio
    async def test_parallel_turns_in_same_thread_finalize_their_own_streams(self):
        adapter, client = _make_adapter()
        client.chat_startStream = AsyncMock(
            side_effect=[
                {"ok": True, "ts": "stream-A"},
                {"ok": True, "ts": "stream-B"},
            ],
        )
        meta_a = {
            **META,
            "reply_to_message_id": "turn-A",
            "scope_id": "T1",
        }
        meta_b = {
            **META,
            "reply_to_message_id": "turn-B",
            "scope_id": "T1",
        }

        await adapter.send_draft("D1", 7, "Preview A", metadata=meta_a)
        await adapter.send_draft("D1", 8, "Preview B", metadata=meta_b)

        result_a = await adapter.send(
            "D1",
            "Final A",
            metadata={**meta_a, "notify": True},
        )
        result_b = await adapter.send(
            "D1",
            "Final B",
            metadata={**meta_b, "notify": True},
        )

        assert result_a.success and result_a.message_id == "stream-A"
        assert result_b.success and result_b.message_id == "stream-B"
        assert [call.kwargs["ts"] for call in client.chat_update.await_args_list] == [
            "stream-A",
            "stream-B",
        ]
        client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_chat_and_turn_identity_remain_workspace_scoped(self):
        adapter, client = _make_adapter()
        client.chat_startStream = AsyncMock(
            side_effect=[
                {"ok": True, "ts": "stream-T1"},
                {"ok": True, "ts": "stream-T2"},
            ],
        )
        meta_t1 = {**META, "reply_to_message_id": "turn-A", "scope_id": "T1"}
        meta_t2 = {**META, "reply_to_message_id": "turn-A", "scope_id": "T2"}

        await adapter.send_draft("D1", 7, "Preview T1", metadata=meta_t1)
        await adapter.send_draft("D1", 8, "Preview T2", metadata=meta_t2)

        result_t2 = await adapter.send(
            "D1", "Final T2", metadata={**meta_t2, "notify": True}
        )
        result_t1 = await adapter.send(
            "D1", "Final T1", metadata={**meta_t1, "notify": True}
        )

        assert result_t2.message_id == "stream-T2"
        assert result_t1.message_id == "stream-T1"
        assert [
            call.kwargs["ts"] for call in client.chat_stopStream.await_args_list
        ] == [
            "stream-T2",
            "stream-T1",
        ]
        client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unique_turn_survives_channel_workspace_map_drift(self):
        adapter, client = _make_adapter()
        selected_teams = []
        adapter._get_client = MagicMock(
            side_effect=lambda _chat_id, team_id="": (
                selected_teams.append(team_id) or client
            )
        )
        metadata = {**META, "reply_to_message_id": "turn-A"}
        adapter._channel_team["D1"] = "T1"
        await adapter.send_draft("D1", 7, "Preview", metadata=metadata)
        adapter._channel_team["D1"] = "T2"

        result = await adapter.send(
            "D1",
            "Authoritative final",
            metadata={**metadata, "notify": True},
        )

        assert result.success and result.message_id == "123.456"
        assert selected_teams and set(selected_teams) == {"T1"}
        client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_persisted_workspace_wins_over_cache_drift_for_final_lifecycle(
        self,
    ):
        adapter, primary = _make_adapter()
        del adapter._get_client
        assert adapter._app is not None
        adapter._app.client = primary
        client_t1 = AsyncMock()
        client_t1.chat_startStream = AsyncMock(return_value={"ts": "stream-T1"})
        client_t1.chat_stopStream = AsyncMock(return_value={"ok": True})
        client_t1.chat_update = AsyncMock(return_value={"ts": "stream-T1"})
        client_t2 = AsyncMock()
        adapter._team_clients = {"T1": client_t1, "T2": client_t2}
        adapter._channel_team["C_SHARED"] = "T2"
        metadata = {
            **META,
            "reply_to_message_id": "turn-A",
            "scope_id": "T1",
        }

        await adapter.send_draft("C_SHARED", 7, "Preview", metadata=metadata)
        result = await adapter.send(
            "C_SHARED",
            "Authoritative final",
            metadata={**META, "reply_to_message_id": "turn-A", "notify": True},
        )

        assert result.success and result.message_id == "stream-T1"
        assert client_t1.chat_startStream.await_args.kwargs["recipient_team_id"] == "T1"
        client_t1.chat_stopStream.assert_awaited_once()
        client_t1.chat_update.assert_awaited_once()
        client_t2.chat_startStream.assert_not_awaited()
        client_t2.chat_stopStream.assert_not_awaited()
        client_t2.chat_update.assert_not_awaited()
        primary.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_workspace_continuation_appends_through_that_workspace(
        self,
    ):
        adapter, primary = _make_adapter()
        del adapter._get_client
        assert adapter._app is not None
        adapter._app.client = primary
        client_t1 = AsyncMock()
        client_t1.chat_startStream = AsyncMock(return_value={"ts": "stream-T1"})
        client_t1.chat_appendStream = AsyncMock(return_value={"ok": True})
        client_t2 = AsyncMock()
        adapter._team_clients = {"T1": client_t1, "T2": client_t2}
        adapter._channel_team["C_SHARED"] = "T2"
        metadata = {**META, "reply_to_message_id": "turn-A", "scope_id": "T1"}

        await adapter.send_draft("C_SHARED", 7, "Preview", metadata=metadata)
        continuation = await adapter.send_draft(
            "C_SHARED", 7, "Preview extended", metadata=metadata
        )

        assert continuation.success and continuation.message_id == "stream-T1"
        client_t1.chat_appendStream.assert_awaited_once()
        client_t2.chat_appendStream.assert_not_awaited()
        primary.chat_appendStream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_continuation_uses_persisted_stream_workspace_when_scope_is_lost(
        self,
    ):
        adapter, primary = _make_adapter()
        del adapter._get_client
        assert adapter._app is not None
        adapter._app.client = primary
        client_t1 = AsyncMock()
        client_t1.chat_startStream = AsyncMock(return_value={"ts": "stream-T1"})
        client_t1.chat_appendStream = AsyncMock(return_value={"ok": True})
        client_t2 = AsyncMock()
        adapter._team_clients = {"T1": client_t1, "T2": client_t2}
        adapter._channel_team["C_SHARED"] = "T2"
        scoped_metadata = {
            **META,
            "reply_to_message_id": "turn-A",
            "scope_id": "T1",
        }
        unscoped_metadata = {**META, "reply_to_message_id": "turn-A"}

        await adapter.send_draft(
            "C_SHARED",
            7,
            "Preview",
            metadata=scoped_metadata,
        )
        continuation = await adapter.send_draft(
            "C_SHARED",
            7,
            "Preview extended",
            metadata=unscoped_metadata,
        )

        assert continuation.success and continuation.message_id == "stream-T1"
        client_t1.chat_appendStream.assert_awaited_once()
        client_t2.chat_appendStream.assert_not_awaited()
        primary.chat_appendStream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_continuation_recovers_persisted_workspace_after_partial_reconnect(
        self,
    ):
        adapter, primary = _make_adapter()
        del adapter._get_client
        assert adapter._app is not None
        adapter._app.client = primary
        client_t1 = AsyncMock()
        client_t1.chat_startStream = AsyncMock(return_value={"ts": "stream-T1"})
        client_t1.chat_appendStream = AsyncMock(return_value={"ok": True})
        # A stale channel cache claims T2 after reconnect, but only the
        # persisted stream's T1 client is connected and may continue it.
        adapter._team_clients = {"T1": client_t1}
        adapter._channel_team["C_SHARED"] = "T2"
        scoped_metadata = {
            **META,
            "reply_to_message_id": "turn-A",
            "scope_id": "T1",
        }
        unscoped_metadata = {**META, "reply_to_message_id": "turn-A"}

        await adapter.send_draft(
            "C_SHARED",
            7,
            "Preview",
            metadata=scoped_metadata,
        )
        continuation = await adapter.send_draft(
            "C_SHARED",
            7,
            "Preview extended",
            metadata=unscoped_metadata,
        )

        assert continuation.success and continuation.message_id == "stream-T1"
        client_t1.chat_appendStream.assert_awaited_once()
        primary.chat_appendStream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_continuation_fails_closed_when_persisted_workspace_client_is_unavailable(
        self,
    ):
        adapter, primary = _make_adapter()
        del adapter._get_client
        assert adapter._app is not None
        adapter._app.client = primary
        client_t1 = AsyncMock()
        client_t1.chat_startStream = AsyncMock(return_value={"ts": "stream-T1"})
        client_t1.chat_appendStream = AsyncMock(return_value={"ok": True})
        client_t2 = AsyncMock()
        adapter._team_clients = {"T1": client_t1}
        scoped_metadata = {
            **META,
            "reply_to_message_id": "turn-A",
            "scope_id": "T1",
        }

        await adapter.send_draft(
            "C_SHARED",
            7,
            "Preview",
            metadata=scoped_metadata,
        )
        adapter._team_clients = {"T2": client_t2}
        adapter._channel_team["C_SHARED"] = "T2"
        continuation = await adapter.send_draft(
            "C_SHARED",
            7,
            "Preview extended",
            metadata={**META, "reply_to_message_id": "turn-A"},
        )

        assert not continuation.success
        assert continuation.error == "native stream workspace client unavailable"
        client_t1.chat_appendStream.assert_not_awaited()
        client_t2.chat_appendStream.assert_not_awaited()
        primary.chat_appendStream.assert_not_awaited()
        assert next(iter(adapter._active_streams.values()))["ts"] == "stream-T1"

    @pytest.mark.asyncio
    async def test_ambiguous_missing_workspace_fails_before_start_stream_api(self):
        adapter, primary = _make_adapter()
        del adapter._get_client
        assert adapter._app is not None
        adapter._app.client = primary
        client_t1 = AsyncMock()
        client_t2 = AsyncMock()
        adapter._team_clients = {"T1": client_t1, "T2": client_t2}
        adapter._channel_teams["C_SHARED"] = {"T1", "T2"}
        adapter._channel_team.pop("C_SHARED", None)

        result = await adapter.send_draft(
            "C_SHARED",
            7,
            "Preview",
            metadata={**META, "reply_to_message_id": "turn-A"},
        )

        assert not result.success
        assert result.error == "ambiguous native stream workspace"
        primary.chat_startStream.assert_not_awaited()
        client_t1.chat_startStream.assert_not_awaited()
        client_t2.chat_startStream.assert_not_awaited()
        assert not adapter._active_streams

    @pytest.mark.asyncio
    async def test_unscoped_continuation_refuses_ambiguous_workspace_streams(self):
        adapter, primary = _make_adapter()
        del adapter._get_client
        assert adapter._app is not None
        adapter._app.client = primary
        client_t1 = AsyncMock()
        client_t1.chat_startStream = AsyncMock(return_value={"ts": "stream-T1"})
        client_t2 = AsyncMock()
        client_t2.chat_startStream = AsyncMock(return_value={"ts": "stream-T2"})
        adapter._team_clients = {"T1": client_t1, "T2": client_t2}
        metadata_t1 = {**META, "reply_to_message_id": "turn-A", "scope_id": "T1"}
        metadata_t2 = {**META, "reply_to_message_id": "turn-A", "scope_id": "T2"}

        await adapter.send_draft("C_SHARED", 7, "Preview T1", metadata=metadata_t1)
        await adapter.send_draft("C_SHARED", 7, "Preview T2", metadata=metadata_t2)
        continuation = await adapter.send_draft(
            "C_SHARED",
            7,
            "Preview extended",
            metadata={**META, "reply_to_message_id": "turn-A"},
        )

        assert not continuation.success
        assert continuation.error == "ambiguous native stream scope"
        client_t1.chat_appendStream.assert_not_awaited()
        client_t2.chat_appendStream.assert_not_awaited()
        client_t1.chat_startStream.assert_awaited_once()
        client_t2.chat_startStream.assert_awaited_once()
        primary.chat_postMessage.assert_not_awaited()
        assert len(adapter._active_streams) == 2

    @pytest.mark.asyncio
    async def test_concurrent_first_frames_start_one_visible_stream(self):
        adapter, client = _make_adapter()
        first_start_entered = asyncio.Event()
        release_start = asyncio.Event()

        async def gated_start(**_kwargs):
            first_start_entered.set()
            await release_start.wait()
            return {"ok": True, "ts": "stream-A"}

        client.chat_startStream = AsyncMock(side_effect=gated_start)
        metadata = {**META, "reply_to_message_id": "turn-A", "scope_id": "T1"}
        first = asyncio.create_task(
            adapter.send_draft("D1", 7, "Preview", metadata=metadata)
        )
        await first_start_entered.wait()
        second = asyncio.create_task(
            adapter.send_draft("D1", 7, "Preview", metadata=metadata)
        )
        await asyncio.sleep(0)
        starts_before_release = client.chat_startStream.await_count
        release_start.set()
        first_result, second_result = await asyncio.gather(first, second)

        assert starts_before_release == 1
        assert client.chat_startStream.await_count == 1
        assert first_result.success and second_result.success
        assert first_result.message_id == second_result.message_id == "stream-A"

    @pytest.mark.asyncio
    async def test_ambiguous_workspace_scope_refuses_authoritative_fallback_post(self):
        adapter, client = _make_adapter()
        meta_t1 = {**META, "reply_to_message_id": "turn-A", "scope_id": "T1"}
        meta_t2 = {**META, "reply_to_message_id": "turn-A", "scope_id": "T2"}
        await adapter.send_draft("D1", 7, "Preview T1", metadata=meta_t1)
        await adapter.send_draft("D1", 8, "Preview T2", metadata=meta_t2)

        result = await adapter.send(
            "D1",
            "Final without workspace",
            metadata={**META, "reply_to_message_id": "turn-A", "notify": True},
        )

        assert not result.success
        assert result.retryable
        assert result.error == "ambiguous native stream scope"
        client.chat_stopStream.assert_not_awaited()
        client.chat_update.assert_not_awaited()
        client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rewritten_authoritative_final_seals_and_updates_same_stream_message(
        self,
    ):
        adapter, client = _make_adapter({"rich_blocks": True})
        streamed_prefix = "Draft: raw *markdown*"
        authoritative_final = "Normalized final response"
        await adapter.send_draft("D1", 7, streamed_prefix, metadata=META)

        result = await adapter.send(
            "D1",
            authoritative_final,
            metadata={**META, "notify": True},
        )

        assert result.success
        assert result.message_id == "123.456"
        assert "markdown_text" not in client.chat_stopStream.await_args.kwargs
        update_kwargs = client.chat_update.await_args.kwargs
        assert update_kwargs["ts"] == "123.456"
        assert update_kwargs["text"] == authoritative_final
        assert update_kwargs["blocks"]
        client.chat_postMessage.assert_not_awaited()
        assert not adapter._active_streams

    @pytest.mark.asyncio
    async def test_rewritten_final_update_failure_reports_unsuccessful_delivery(self):
        adapter, client = _make_adapter()
        await adapter.send_draft("D1", 7, "Draft preview", metadata=META)
        client.chat_update = AsyncMock(side_effect=Exception("update failed"))

        result = await adapter.send(
            "D1",
            "Normalized final response",
            metadata={**META, "notify": True},
        )

        assert not result.success
        assert result.retryable
        assert result.message_id == "123.456"
        assert result.error == "update failed"
        client.chat_postMessage.assert_not_awaited()
        assert next(iter(adapter._active_streams.values()))["stopped"] is True

        client.chat_update = AsyncMock(return_value={"ok": True, "ts": "123.456"})
        retry = await adapter.send(
            "D1",
            "Normalized final response",
            metadata={**META, "notify": True},
        )

        assert retry.success
        assert retry.message_id == "123.456"
        assert client.chat_stopStream.await_count == 1
        client.chat_postMessage.assert_not_awaited()
        assert not adapter._active_streams

    @pytest.mark.asyncio
    async def test_final_send_seals_stream_no_duplicate_post(self):
        adapter, client = _make_adapter()
        await adapter.send_draft("D1", 7, "Hello wo", metadata=META)
        result = await adapter.send("D1", "Hello world, done.", metadata=META)
        assert result.success
        assert result.message_id == "123.456"
        kwargs = client.chat_stopStream.await_args.kwargs
        assert kwargs["markdown_text"] == "rld, done."
        client.chat_postMessage.assert_not_awaited()
        assert not adapter._active_streams

    @pytest.mark.asyncio
    async def test_final_send_equal_content_seals_without_delta(self):
        adapter, client = _make_adapter()
        await adapter.send_draft("D1", 7, "Hello world", metadata=META)
        result = await adapter.send("D1", "Hello world", metadata=META)
        assert result.success
        kwargs = client.chat_stopStream.await_args.kwargs
        assert "markdown_text" not in kwargs
        client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unrelated_send_passes_through(self):
        adapter, client = _make_adapter()
        await adapter.send_draft("D1", 7, "Streaming text here", metadata=META)
        result = await adapter.send("D1", "Unrelated notice", metadata=META)
        assert result.success
        client.chat_postMessage.assert_awaited()
        # Stream stays open for its own finalization.
        assert adapter._active_streams

    @pytest.mark.asyncio
    async def test_stop_stream_failure_preserves_stream_for_retry_without_post(self):
        adapter, client = _make_adapter()
        await adapter.send_draft("D1", 7, "Hello", metadata=META)
        client.chat_stopStream = AsyncMock(side_effect=Exception("boom"))
        result = await adapter.send(
            "D1",
            "Hello world",
            metadata={**META, "notify": True},
        )

        assert not result.success
        assert result.retryable
        assert result.message_id == "123.456"
        client.chat_postMessage.assert_not_awaited()
        assert adapter._active_streams

        client.chat_stopStream = AsyncMock(return_value={"ok": True})
        retry = await adapter.send(
            "D1",
            "Hello world",
            metadata={**META, "notify": True},
        )

        assert retry.success
        assert retry.message_id == "123.456"
        client.chat_postMessage.assert_not_awaited()
        assert not adapter._active_streams

    @pytest.mark.asyncio
    async def test_consumer_retries_failed_stop_on_same_stream_without_post(self):
        adapter, client = _make_adapter()
        client.chat_stopStream = AsyncMock(
            side_effect=[Exception("lost acknowledgement"), {"ok": True}],
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "D1",
            StreamConsumerConfig(
                transport="auto",
                chat_type="dm",
                edit_interval=0.01,
                buffer_threshold=1,
                cursor="",
            ),
            metadata=META,
        )

        task = asyncio.create_task(consumer.run())
        consumer.on_delta("Hello world")
        await asyncio.sleep(0.05)
        consumer.finish()
        await task

        assert client.chat_stopStream.await_count == 2
        client.chat_postMessage.assert_not_awaited()
        assert not adapter._active_streams
        assert consumer.final_content_delivered is True

    @pytest.mark.asyncio
    async def test_abandon_uses_persisted_workspace_after_metadata_and_cache_drift(
        self,
    ):
        adapter, primary = _make_adapter()
        del adapter._get_client
        assert adapter._app is not None
        adapter._app.client = primary
        client_t1 = AsyncMock()
        client_t1.chat_startStream = AsyncMock(return_value={"ts": "stream-T1"})
        client_t1.chat_stopStream = AsyncMock(return_value={"ok": True})
        client_t2 = AsyncMock()
        adapter._team_clients = {"T1": client_t1, "T2": client_t2}
        adapter.stop_typing = AsyncMock()
        metadata = {
            **META,
            "reply_to_message_id": "turn-A",
            "scope_id": "T1",
        }
        await adapter.send_draft("C_SHARED", 7, "Partial answer", metadata=metadata)
        adapter._channel_team["C_SHARED"] = "T2"
        unscoped_metadata = {**META, "reply_to_message_id": "turn-A"}

        await adapter.abandon_open_draft(
            "C_SHARED",
            "Partial answer",
            metadata=unscoped_metadata,
        )

        client_t1.chat_stopStream.assert_awaited_once()
        client_t2.chat_stopStream.assert_not_awaited()
        primary.chat_stopStream.assert_not_awaited()
        primary.chat_postMessage.assert_not_awaited()
        adapter.stop_typing.assert_awaited_once_with(
            "C_SHARED", metadata=unscoped_metadata
        )
        assert not adapter._active_streams

    @pytest.mark.asyncio
    async def test_rich_blocks_applied_after_seal(self):
        adapter, client = _make_adapter({"rich_blocks": True})
        rich = "# Title\n\nbody text"
        await adapter.send_draft("D1", 7, rich[:5], metadata=META)
        result = await adapter.send("D1", rich, metadata=META)
        assert result.success
        client.chat_update.assert_awaited()
        assert client.chat_update.await_args.kwargs["blocks"]


class TestDisconnectCleanup:
    @pytest.mark.asyncio
    async def test_disconnect_seals_dangling_streams(self):
        adapter, client = _make_adapter()
        await adapter.send_draft("D1", 7, "Dangling", metadata=META)
        adapter._stop_socket_mode_handler = AsyncMock()
        adapter._release_platform_lock = MagicMock()
        await adapter.disconnect()
        client.chat_stopStream.assert_awaited()
        assert not adapter._active_streams
