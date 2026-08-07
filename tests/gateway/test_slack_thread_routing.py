"""Behavior specs for Slack per-thread strict-mention routing (AN-1883).

These tests intentionally describe the desired routing contract before the
Slack adapter implementation exists. They are meant to be reviewed first, then
implemented with TDD one behavior slice at a time.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionEntry, SessionSource, build_session_key


# ---------------------------------------------------------------------------
# Minimal Slack SDK mocks so SlackAdapter can be imported in test envs without
# slack-bolt installed. Mirrors tests/gateway/test_slack.py.
# ---------------------------------------------------------------------------


def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return

    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock

    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock

    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        (
            "slack_bolt.adapter.socket_mode.async_handler",
            slack_bolt.adapter.socket_mode.async_handler,
        ),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)

    sys.modules.setdefault("aiohttp", MagicMock())


_ensure_slack_mock()

import plugins.platforms.slack.adapter as _slack_mod  # noqa: E402

_slack_mod.SLACK_AVAILABLE = True

from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402


BOT_USER_ID = "U_BOT"
USER_ID = "U_USER"
OTHER_USER_ID = "U_OTHER"
TEAM_ID = "T1"
OTHER_TEAM_ID = "T2"
CHANNEL_ID = "C1"
OTHER_CHANNEL_ID = "C2"
THREAD_TS = "1710000000.000001"
MESSAGE_TS = "1710000001.000002"


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture()
def adapter(hermes_home):
    config = PlatformConfig(
        enabled=True,
        token="***",
        extra={
            "allowed_channels": "",
            "require_mention": True,
            "thread_routing": {
                "enabled": True,
                "state_file": "slack/thread_routing_state.json",
                "suppress_other_user_mentions": True,
                "stop_patterns": [
                    r"(?<!\w)не\s+отвечай(?!\w)",
                    r"\bstop\s+(responding|replying)\b",
                ],
                "resume_patterns": [
                    r"(?<!\w)можешь\s+(снова\s+)?отвечать(?!\w)",
                    r"\bunmute\b",
                ],
            },
        },
    )
    slack_adapter = SlackAdapter(config)
    slack_adapter._app = MagicMock()
    slack_adapter._app.client = AsyncMock()
    slack_adapter._app.client.users_info.return_value = {
        "user": {"name": "test-user", "profile": {"display_name": "Test User"}}
    }
    slack_adapter._app.client.conversations_replies.return_value = {"messages": []}
    slack_adapter._bot_user_id = BOT_USER_ID
    slack_adapter._team_bot_user_ids = {TEAM_ID: BOT_USER_ID, OTHER_TEAM_ID: BOT_USER_ID}
    slack_adapter._running = True
    slack_adapter.handle_message = AsyncMock()
    return slack_adapter


def state_file(hermes_home: Path) -> Path:
    return hermes_home / "slack" / "thread_routing_state.json"


def thread_key(team=TEAM_ID, channel=CHANNEL_ID, thread_ts=THREAD_TS) -> str:
    return f"{team}:{channel}:{thread_ts}"


def read_state(hermes_home: Path) -> dict:
    path = state_file(hermes_home)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_strict_state(
    hermes_home: Path,
    *,
    team=TEAM_ID,
    channel=CHANNEL_ID,
    thread_ts=THREAD_TS,
    set_at=1780000000.0,
) -> None:
    path = state_file(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = thread_key(team=team, channel=channel, thread_ts=thread_ts)
    path.write_text(
        json.dumps(
            {
                key: {
                    "mode": "strict_mention",
                    "team_id": team,
                    "channel_id": channel,
                    "thread_ts": thread_ts,
                    "set_at": set_at,
                    "set_by_user": USER_ID,
                    "set_by_message_ts": MESSAGE_TS,
                    "matched_pattern": r"(?<!\w)не\s+отвечай(?!\w)",
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def make_event(
    text: str,
    *,
    team=TEAM_ID,
    channel=CHANNEL_ID,
    user=USER_ID,
    thread_ts=THREAD_TS,
    ts=MESSAGE_TS,
    channel_type="channel",
    files=None,
) -> dict:
    event = {
        "type": "message",
        "text": text,
        "user": user,
        "channel": channel,
        "channel_type": channel_type,
        "team": team,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    if files is not None:
        event["files"] = files
    return event


def set_active_session(adapter, *, team=TEAM_ID, channel=CHANNEL_ID, thread_ts=THREAD_TS):
    """Mark exactly one team/channel/thread as an active agent thread.

    This helper models a persisted session-backed active thread only. It must
    not seed SlackAdapter's in-memory active-thread implementation details,
    otherwise the tests stop proving session-backed eligibility.
    """
    expected_thread_ts = thread_ts

    def has_active_session(
        *, channel_id, thread_ts: str, user_id: str, team_id=None, chat_type="group"
    ):
        return (
            (team_id is None or team_id == team)
            and channel_id == channel
            and thread_ts == expected_thread_ts
        )

    adapter._has_active_session_for_thread = MagicMock(side_effect=has_active_session)


def install_session_store_entry(
    adapter,
    *,
    team: str | None = TEAM_ID,
    channel=CHANNEL_ID,
    thread_ts=THREAD_TS,
    user=USER_ID,
):
    """Install a real-shaped SessionStore entry for Slack thread lookups.

    Passing team=None models legacy/unscoped entries created before Slack
    workspace metadata was persisted on SessionSource.origin.guild_id.
    """
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id=channel,
        chat_type="group",
        user_id=user,
        thread_id=thread_ts,
        guild_id=team,
    )
    session_key = build_session_key(
        source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    now = datetime.now()
    entry = SessionEntry(
        session_key=session_key,
        session_id="session-1",
        created_at=now,
        updated_at=now,
        origin=source,
        platform=Platform.SLACK,
        chat_type="group",
    )
    adapter._session_store = SimpleNamespace(
        config=SimpleNamespace(
            group_sessions_per_user=True,
            thread_sessions_per_user=False,
        ),
        _entries={session_key: entry},
        _ensure_loaded=MagicMock(),
    )
    return entry


class TestNormalModeBaseline:
    @pytest.mark.asyncio
    async def test_direct_bot_mention_routes_in_normal_thread(self, adapter):
        await adapter._handle_slack_message(make_event(f"<@{BOT_USER_ID}> проверь"))

        adapter.handle_message.assert_awaited_once()
        msg_event = adapter.handle_message.await_args.args[0]
        assert "проверь" in msg_event.text
        assert f"<@{BOT_USER_ID}>" not in msg_event.text

    @pytest.mark.asyncio
    async def test_unmentioned_message_in_inactive_channel_thread_is_ignored(
        self, adapter, hermes_home
    ):
        await adapter._handle_slack_message(make_event("обычное сообщение"))

        adapter.handle_message.assert_not_awaited()
        assert read_state(hermes_home) == {}

    @pytest.mark.asyncio
    async def test_active_agent_thread_auto_follows_plain_message_in_normal_mode(
        self, adapter
    ):
        set_active_session(adapter)

        await adapter._handle_slack_message(make_event("обычный ответ в активном треде"))

        adapter.handle_message.assert_awaited_once()


class TestConversationFlow:
    @pytest.mark.asyncio
    async def test_direct_call_then_unmentioned_stop_then_plain_message_is_suppressed(
        self, adapter, hermes_home
    ):
        await adapter._handle_slack_message(
            make_event(f"<@{BOT_USER_ID}> помоги разобраться")
        )
        adapter.handle_message.assert_awaited_once()

        # The first explicit call establishes that this team/channel/thread is
        # an active agent conversation. The stop message below is intentionally
        # unmentioned: it should be accepted only because the thread is scoped
        # active for the agent.
        set_active_session(adapter)
        adapter.handle_message.reset_mock()

        await adapter._handle_slack_message(
            make_event("не отвечай на сообщения", ts="1710000002.000003")
        )

        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"
        adapter.handle_message.assert_not_awaited()

        await adapter._handle_slack_message(
            make_event("сообщение после stop без mention", ts="1710000003.000004")
        )

        adapter.handle_message.assert_not_awaited()
        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"

    @pytest.mark.asyncio
    async def test_direct_bot_mention_during_strict_mode_replies_once_but_does_not_wake_thread(
        self, adapter, hermes_home
    ):
        set_active_session(adapter)

        await adapter._handle_slack_message(
            make_event("не отвечай на сообщения", ts="1710000002.000003")
        )
        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"
        adapter.handle_message.assert_not_awaited()

        await adapter._handle_slack_message(
            make_event(f"<@{BOT_USER_ID}> ответь только на это", ts="1710000003.000004")
        )
        adapter.handle_message.assert_awaited_once()
        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"

        adapter.handle_message.reset_mock()
        await adapter._handle_slack_message(
            make_event("последующее сообщение без mention", ts="1710000004.000005")
        )

        adapter.handle_message.assert_not_awaited()
        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"

    @pytest.mark.asyncio
    async def test_unmentioned_resume_keeps_strict_but_direct_resume_wakes_thread_again(
        self, adapter, hermes_home
    ):
        set_active_session(adapter)

        await adapter._handle_slack_message(
            make_event("не отвечай на сообщения", ts="1710000002.000003")
        )
        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"
        adapter.handle_message.assert_not_awaited()

        await adapter._handle_slack_message(
            make_event("сообщение пока strict", ts="1710000003.000004")
        )
        adapter.handle_message.assert_not_awaited()

        await adapter._handle_slack_message(
            make_event("можешь снова отвечать", ts="1710000004.000005")
        )
        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"
        adapter.handle_message.assert_not_awaited()

        await adapter._handle_slack_message(
            make_event(f"<@{BOT_USER_ID}> можешь снова отвечать", ts="1710000005.000006")
        )
        assert thread_key() not in read_state(hermes_home)
        adapter.handle_message.assert_awaited_once()

        adapter.handle_message.reset_mock()
        await adapter._handle_slack_message(
            make_event("сообщение после пробуждения", ts="1710000006.000007")
        )

        adapter.handle_message.assert_awaited_once()


class TestStopRequests:
    @pytest.mark.asyncio
    async def test_stop_with_direct_bot_mention_sets_strict_and_suppresses(
        self, adapter, hermes_home
    ):
        await adapter._handle_slack_message(
            make_event(f"<@{BOT_USER_ID}> не отвечай на сообщения")
        )

        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unmentioned_stop_in_scoped_active_agent_thread_sets_strict_and_suppresses(
        self, adapter, hermes_home
    ):
        set_active_session(adapter)
        assert thread_key() not in adapter._slack_active_thread_keys

        await adapter._handle_slack_message(make_event("не отвечай на сообщения"))

        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unmentioned_stop_in_unrelated_public_chatter_does_not_set_strict(
        self, adapter, hermes_home
    ):
        await adapter._handle_slack_message(make_event("не отвечай на сообщения"))

        assert read_state(hermes_home) == {}
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unmentioned_stop_does_not_use_timestamp_only_mentioned_threads_for_scope(
        self, adapter, hermes_home
    ):
        adapter._mentioned_threads.add(THREAD_TS)

        await adapter._handle_slack_message(
            make_event("не отвечай на сообщения", channel=OTHER_CHANNEL_ID)
        )

        assert read_state(hermes_home) == {}
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unmentioned_stop_requires_team_scoped_active_thread(
        self, adapter, hermes_home
    ):
        set_active_session(adapter, team=TEAM_ID, channel=CHANNEL_ID, thread_ts=THREAD_TS)

        await adapter._handle_slack_message(
            make_event("не отвечай на сообщения", team=OTHER_TEAM_ID, channel=CHANNEL_ID)
        )

        assert read_state(hermes_home) == {}
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unscoped_legacy_session_does_not_grant_unmentioned_stop(
        self, adapter, hermes_home
    ):
        install_session_store_entry(adapter, team=None)

        await adapter._handle_slack_message(make_event("не отвечай на сообщения"))

        assert read_state(hermes_home) == {}
        adapter.handle_message.assert_not_awaited()


class TestStrictMode:
    @pytest.mark.asyncio
    async def test_plain_message_in_strict_thread_is_suppressed(
        self, adapter, hermes_home
    ):
        write_strict_state(hermes_home)
        set_active_session(adapter)

        await adapter._handle_slack_message(make_event("обычное сообщение"))

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_bot_mention_in_strict_thread_routes_without_clearing_strict(
        self, adapter, hermes_home
    ):
        write_strict_state(hermes_home)

        await adapter._handle_slack_message(make_event(f"<@{BOT_USER_ID}> ответь на это"))

        adapter.handle_message.assert_awaited_once()
        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"

    @pytest.mark.asyncio
    async def test_other_user_mention_in_strict_thread_is_suppressed_without_bot_mention(
        self, adapter, hermes_home
    ):
        write_strict_state(hermes_home)
        set_active_session(adapter)

        await adapter._handle_slack_message(make_event(f"<@{OTHER_USER_ID}> можешь проверить?"))

        adapter.handle_message.assert_not_awaited()


class TestResumeRequests:
    @pytest.mark.asyncio
    async def test_unmentioned_resume_in_strict_thread_does_not_clear_strict_state(
        self, adapter, hermes_home
    ):
        write_strict_state(hermes_home)

        await adapter._handle_slack_message(make_event("можешь снова отвечать"))

        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_with_direct_bot_mention_clears_strict_and_routes(
        self, adapter, hermes_home
    ):
        write_strict_state(hermes_home)

        await adapter._handle_slack_message(
            make_event(f"<@{BOT_USER_ID}> можешь снова отвечать")
        )

        assert thread_key() not in read_state(hermes_home)
        adapter.handle_message.assert_awaited_once()


class TestOtherUserMentionGuard:
    @pytest.mark.asyncio
    async def test_other_user_mention_in_active_agent_thread_is_suppressed(
        self, adapter
    ):
        set_active_session(adapter)
        assert thread_key() not in adapter._slack_active_thread_keys

        await adapter._handle_slack_message(make_event(f"<@{OTHER_USER_ID}> посмотри пожалуйста"))

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_user_mention_requires_team_scoped_active_thread(
        self, adapter
    ):
        set_active_session(adapter, team=TEAM_ID, channel=CHANNEL_ID, thread_ts=THREAD_TS)

        await adapter._handle_slack_message(
            make_event(f"<@{OTHER_USER_ID}> посмотри пожалуйста", team=OTHER_TEAM_ID)
        )

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unscoped_legacy_session_does_not_auto_follow_other_user_mention(
        self, adapter
    ):
        install_session_store_entry(adapter, team=None)

        await adapter._handle_slack_message(make_event(f"<@{OTHER_USER_ID}> посмотри пожалуйста"))

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_user_mention_suppression_defaults_on_when_flag_omitted(
        self, adapter
    ):
        adapter.config.extra["thread_routing"].pop("suppress_other_user_mentions")
        set_active_session(adapter)

        await adapter._handle_slack_message(make_event(f"<@{OTHER_USER_ID}> посмотри пожалуйста"))

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bot_mention_and_other_user_mention_routes_because_bot_is_explicitly_addressed(
        self, adapter
    ):
        await adapter._handle_slack_message(
            make_event(f"<@{BOT_USER_ID}> <@{OTHER_USER_ID}> что думаешь?")
        )

        adapter.handle_message.assert_awaited_once()


class TestCommandGuard:
    @pytest.mark.asyncio
    async def test_slash_stop_command_does_not_set_thread_strict_state(
        self, adapter, hermes_home
    ):
        set_active_session(adapter)

        await adapter._handle_slack_message(make_event("/stop responding"))

        assert read_state(hermes_home) == {}
        adapter.handle_message.assert_awaited_once()
        msg_event = adapter.handle_message.await_args.args[0]
        assert msg_event.text.startswith("/stop")

    @pytest.mark.asyncio
    async def test_bang_stop_command_does_not_set_thread_strict_state(
        self, adapter, hermes_home
    ):
        set_active_session(adapter)

        await adapter._handle_slack_message(make_event("!stop responding"))

        assert read_state(hermes_home) == {}
        adapter.handle_message.assert_awaited_once()
        msg_event = adapter.handle_message.await_args.args[0]
        assert msg_event.text.startswith("/stop")

    @pytest.mark.asyncio
    async def test_mentioned_bang_stop_command_does_not_set_thread_strict_state(
        self, adapter, hermes_home
    ):
        set_active_session(adapter)

        await adapter._handle_slack_message(
            make_event(f"<@{BOT_USER_ID}> !stop responding")
        )

        assert read_state(hermes_home) == {}
        adapter.handle_message.assert_awaited_once()
        msg_event = adapter.handle_message.await_args.args[0]
        assert msg_event.text.startswith("/stop")


class TestPersistenceAndIsolation:
    def test_team_scoped_session_lookup_accepts_matching_origin(self, adapter):
        install_session_store_entry(adapter, team=TEAM_ID)

        assert (
            adapter._has_active_session_for_thread(
                channel_id=CHANNEL_ID,
                thread_ts=THREAD_TS,
                user_id=USER_ID,
                team_id=TEAM_ID,
            )
            is True
        )

    def test_team_scoped_session_lookup_rejects_legacy_unscoped_origin(self, adapter):
        install_session_store_entry(adapter, team=None)

        assert (
            adapter._has_active_session_for_thread(
                channel_id=CHANNEL_ID,
                thread_ts=THREAD_TS,
                user_id=USER_ID,
                team_id=TEAM_ID,
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_strict_state_is_isolated_by_team_channel_and_thread(
        self, adapter, hermes_home
    ):
        write_strict_state(hermes_home, team=TEAM_ID, channel=CHANNEL_ID, thread_ts=THREAD_TS)
        set_active_session(adapter, team=TEAM_ID, channel=OTHER_CHANNEL_ID, thread_ts=THREAD_TS)

        await adapter._handle_slack_message(
            make_event("same timestamp, different channel", channel=OTHER_CHANNEL_ID)
        )

        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_strict_state_survives_adapter_recreation(self, adapter, hermes_home):
        await adapter._handle_slack_message(
            make_event(f"<@{BOT_USER_ID}> не отвечай на сообщения")
        )
        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"

        recreated = SlackAdapter(adapter.config)
        recreated._app = MagicMock()
        recreated._app.client = AsyncMock()
        recreated._app.client.users_info.return_value = {
            "user": {"name": "test-user", "profile": {"display_name": "Test User"}}
        }
        recreated._app.client.conversations_replies.return_value = {"messages": []}
        recreated._bot_user_id = BOT_USER_ID
        recreated._team_bot_user_ids = {TEAM_ID: BOT_USER_ID}
        recreated.handle_message = AsyncMock()
        set_active_session(recreated)

        await recreated._handle_slack_message(make_event("обычное сообщение после рестарта"))

        recreated.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_corrupt_thread_routing_json_is_ignored_safely(
        self, adapter, hermes_home
    ):
        path = state_file(hermes_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")

        await adapter._handle_slack_message(make_event(f"<@{BOT_USER_ID}> проверь"))

        adapter.handle_message.assert_awaited_once()


class TestSuppressionSideEffects:
    @pytest.mark.asyncio
    async def test_strict_suppressed_plain_message_does_not_fetch_context_download_files_or_call_agent(
        self, adapter, hermes_home
    ):
        write_strict_state(hermes_home)
        set_active_session(adapter)
        adapter._fetch_thread_context = AsyncMock(return_value="thread context")
        adapter._download_slack_file = AsyncMock(return_value="/tmp/downloaded.txt")
        adapter._download_slack_file_bytes = AsyncMock(return_value=b"downloaded")
        files = [
            {
                "id": "F1",
                "name": "note.txt",
                "mimetype": "text/plain",
                "filetype": "text",
                "url_private_download": "https://files.slack.test/F1",
                "size": 12,
            }
        ]

        await adapter._handle_slack_message(make_event("обычное сообщение", files=files))

        adapter._fetch_thread_context.assert_not_awaited()
        adapter._download_slack_file.assert_not_awaited()
        adapter._download_slack_file_bytes.assert_not_awaited()
        adapter.handle_message.assert_not_awaited()


class TestConfigAndPatterns:
    def test_slack_thread_routing_config_is_bridged_into_platform_extra(self, tmp_path, monkeypatch):
        from gateway.config import Platform, load_gateway_config

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "slack:\n"
            "  require_mention: true\n"
            "  thread_routing:\n"
            "    enabled: true\n"
            "    stop_patterns:\n"
            "      - quiet please\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        slack_extra = config.platforms[Platform.SLACK].extra
        assert slack_extra["thread_routing"]["enabled"] is True
        assert slack_extra["thread_routing"]["stop_patterns"] == ["quiet please"]
