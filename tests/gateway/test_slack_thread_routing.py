"""Behavior specs for Slack per-thread strict-mention routing (AN-1883).

These tests intentionally describe the desired routing contract before the
Slack adapter implementation exists. They are meant to be reviewed first, then
implemented with TDD one behavior slice at a time.
"""

import asyncio
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType
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
from plugins.platforms.slack.thread_routing import (  # noqa: E402
    RoutingDecision,
    RoutingDisposition,
)


BOT_USER_ID = "U_BOT"
BOT_ID = "B_BOT"
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
    slack_adapter._team_clients = {
        TEAM_ID: slack_adapter._app.client,
        OTHER_TEAM_ID: slack_adapter._app.client,
    }
    slack_adapter._team_bot_user_ids = {TEAM_ID: BOT_USER_ID, OTHER_TEAM_ID: BOT_USER_ID}
    slack_adapter._team_bot_ids = {TEAM_ID: BOT_ID, OTHER_TEAM_ID: "B_SECONDARY"}
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


class TestWorkspaceScopedBotGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mention",
        (f"<@{BOT_USER_ID}>", f"<@{BOT_USER_ID}|hermes>"),
    )
    async def test_primary_workspace_peer_bot_semantic_mentions_route(
        self, adapter, mention
    ):
        adapter.config.extra["allow_bots"] = "mentions"
        event = make_event(f"{mention} check this", user="U_PEER_BOT")
        event["bot_id"] = "B_PEER"

        await adapter._handle_slack_message(event)

        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_secondary_workspace_peer_bot_mention_routes(self, adapter):
        adapter.config.extra["allow_bots"] = "mentions"
        secondary_bot_id = "U_SECONDARY_BOT"
        adapter._team_bot_user_ids[OTHER_TEAM_ID] = secondary_bot_id
        event = make_event(
            f"<@{secondary_bot_id}> check this",
            team=OTHER_TEAM_ID,
            user="U_PEER_BOT",
        )
        event["bot_id"] = "B_PEER"

        await adapter._handle_slack_message(event)

        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_secondary_workspace_blockkit_peer_bot_mention_routes(self, adapter):
        adapter.config.extra["allow_bots"] = "mentions"
        secondary_bot_id = "U_SECONDARY_BOT"
        adapter._team_bot_user_ids[OTHER_TEAM_ID] = secondary_bot_id
        event = make_event(
            "Release notification",
            team=OTHER_TEAM_ID,
            user="U_PEER_BOT",
        )
        event.update({
            "bot_id": "B_PEER",
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [
                                {"type": "user", "user_id": secondary_bot_id}
                            ],
                        }
                    ],
                }
            ],
        })

        await adapter._handle_slack_message(event)

        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_secondary_workspace_own_bot_message_is_suppressed(self, adapter):
        adapter.config.extra["allow_bots"] = "all"
        secondary_bot_id = "U_SECONDARY_BOT"
        adapter._team_bot_user_ids[OTHER_TEAM_ID] = secondary_bot_id
        event = make_event(
            f"<@{secondary_bot_id}> self echo",
            team=OTHER_TEAM_ID,
            user=secondary_bot_id,
        )
        event["bot_id"] = "B_SELF"

        await adapter._handle_slack_message(event)

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cached_secondary_workspace_own_bot_message_is_suppressed(
        self, adapter
    ):
        adapter.config.extra["allow_bots"] = "all"
        secondary_bot_id = "U_SECONDARY_BOT"
        adapter._team_bot_user_ids[OTHER_TEAM_ID] = secondary_bot_id
        adapter._channel_team[CHANNEL_ID] = OTHER_TEAM_ID
        event = make_event(
            f"<@{secondary_bot_id}> self echo",
            team="",
            user=secondary_bot_id,
        )
        event["bot_id"] = "B_SELF"

        await adapter._handle_slack_message(event)

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_userless_secondary_workspace_own_bot_message_is_suppressed(
        self, adapter
    ):
        adapter.config.extra.update({"allow_bots": "all", "require_mention": False})
        event = make_event("self echo", team=OTHER_TEAM_ID, user="")
        event.update({"subtype": "bot_message", "bot_id": "B_SECONDARY"})

        await adapter._handle_slack_message(event)

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_userless_workspace_peer_bot_message_routes_under_allow_all(
        self, adapter
    ):
        adapter.config.extra.update({"allow_bots": "all", "require_mention": False})
        event = make_event("peer update", team=OTHER_TEAM_ID, user="")
        event.update({"subtype": "bot_message", "bot_id": "B_PEER"})

        await adapter._handle_slack_message(event)

        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("allow_bots", ("all", "mentions"))
    async def test_userless_bot_event_is_suppressed_when_own_bot_id_is_unresolved(
        self, adapter, allow_bots
    ):
        adapter.config.extra.update(
            {"allow_bots": allow_bots, "require_mention": False}
        )
        adapter._team_bot_ids.pop(OTHER_TEAM_ID)
        event = make_event(
            f"<@{BOT_USER_ID}> unresolved identity",
            team=OTHER_TEAM_ID,
            user="",
        )
        event.update({"subtype": "bot_message", "bot_id": "B_UNRESOLVED"})

        await adapter._handle_slack_message(event)

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel_scope", ("missing", "ambiguous"))
    async def test_unresolved_workspace_bot_event_is_suppressed(
        self, adapter, channel_scope
    ):
        adapter.config.extra.update({"allow_bots": "all", "require_mention": False})
        secondary_bot_id = "U_SECONDARY_BOT"
        adapter._team_bot_user_ids[OTHER_TEAM_ID] = secondary_bot_id
        if channel_scope == "ambiguous":
            adapter._remember_channel_team(CHANNEL_ID, TEAM_ID)
            adapter._remember_channel_team(CHANNEL_ID, OTHER_TEAM_ID)
            assert CHANNEL_ID not in adapter._channel_team

        event = make_event("self echo", team="", user=secondary_bot_id)
        event["bot_id"] = "B_SELF"

        await adapter._handle_slack_message(event)

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel_scope", ("missing", "ambiguous"))
    @pytest.mark.parametrize("routing_gate", ("require_mention", "allowed_channels"))
    async def test_unresolved_workspace_human_channel_event_is_suppressed_before_hydration(
        self, adapter, channel_scope, routing_gate
    ):
        adapter._fetch_thread_context = AsyncMock(return_value="thread context")
        adapter._download_slack_file = AsyncMock(return_value="/tmp/downloaded.txt")
        adapter._download_slack_file_bytes = AsyncMock(return_value=b"downloaded")
        if routing_gate == "allowed_channels":
            adapter.config.extra.update({
                "allowed_channels": [OTHER_CHANNEL_ID],
                "require_mention": False,
            })
        if channel_scope == "ambiguous":
            adapter._remember_channel_team(CHANNEL_ID, TEAM_ID)
            adapter._remember_channel_team(CHANNEL_ID, OTHER_TEAM_ID)
            assert CHANNEL_ID not in adapter._channel_team
            adapter._team_bot_user_ids = {TEAM_ID: BOT_USER_ID}
            adapter._team_bot_ids = {TEAM_ID: BOT_ID}

        await adapter._handle_slack_message(
            make_event(
                "ordinary unmentioned human text",
                team="",
                files=[
                    {
                        "id": "F1",
                        "name": "note.txt",
                        "mimetype": "text/plain",
                        "filetype": "text",
                        "url_private_download": "https://files.slack.test/F1",
                        "size": 12,
                    }
                ],
            )
        )

        adapter._app.client.users_info.assert_not_awaited()
        adapter._fetch_thread_context.assert_not_awaited()
        adapter._download_slack_file.assert_not_awaited()
        adapter._download_slack_file_bytes.assert_not_awaited()
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_body_only_workspace_scope_uses_scoped_client_for_bot_lookup(
        self, adapter
    ):
        secondary_bot_uid = "U_SECONDARY_BOT"
        primary_client = adapter._app.client
        secondary_client = AsyncMock()
        secondary_client.users_info.return_value = {
            "user": {
                "name": "secondary-user",
                "profile": {"display_name": "Secondary User"},
            }
        }
        secondary_client.conversations_replies.return_value = {"messages": []}
        adapter._team_clients = {
            TEAM_ID: primary_client,
            OTHER_TEAM_ID: secondary_client,
        }
        adapter._team_bot_user_ids[OTHER_TEAM_ID] = secondary_bot_uid

        await adapter._handle_slack_message(
            make_event(f"<@{secondary_bot_uid}> scoped request", team=""),
            payload={"team_id": OTHER_TEAM_ID},
        )

        primary_client.users_info.assert_not_awaited()
        secondary_client.users_info.assert_awaited_once_with(user=USER_ID)
        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unique_registered_workspace_scope_is_propagated_to_lookup_and_source(
        self, adapter
    ):
        secondary_bot_uid = "U_SECONDARY_BOT"
        primary_client = adapter._app.client
        secondary_client = AsyncMock()
        secondary_client.users_info.return_value = {
            "user": {
                "name": "secondary-user",
                "profile": {"display_name": "Secondary User"},
            }
        }
        secondary_client.conversations_replies.return_value = {"messages": []}
        adapter._team_clients = {OTHER_TEAM_ID: secondary_client}
        adapter._team_bot_user_ids = {OTHER_TEAM_ID: secondary_bot_uid}
        adapter._team_bot_ids = {OTHER_TEAM_ID: "B_SECONDARY"}

        await adapter._handle_slack_message(
            make_event(f"<@{secondary_bot_uid}> scoped request", team="")
        )

        primary_client.users_info.assert_not_awaited()
        assert secondary_client.users_info.await_count >= 1
        adapter.handle_message.assert_awaited_once()
        msg_event = adapter.handle_message.await_args.args[0]
        assert msg_event.source.scope_id == OTHER_TEAM_ID

    @pytest.mark.asyncio
    async def test_explicit_workspace_without_registered_client_is_suppressed(
        self, adapter
    ):
        secondary_bot_uid = "U_SECONDARY_BOT"
        primary_client = adapter._app.client
        adapter._team_clients = {TEAM_ID: primary_client}
        adapter._team_bot_user_ids[OTHER_TEAM_ID] = secondary_bot_uid
        adapter._fetch_thread_context = AsyncMock(return_value="thread context")

        await adapter._handle_slack_message(
            make_event(f"<@{secondary_bot_uid}> scoped request", team=""),
            payload={"team_id": OTHER_TEAM_ID},
        )

        primary_client.users_info.assert_not_awaited()
        primary_client.conversations_replies.assert_not_awaited()
        adapter._fetch_thread_context.assert_not_awaited()
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_workspace_with_empty_client_registry_and_known_identities_is_suppressed(
        self, adapter
    ):
        primary_client = adapter._app.client
        adapter._team_clients = {}
        adapter._team_bot_user_ids = {
            TEAM_ID: BOT_USER_ID,
            OTHER_TEAM_ID: "U_SECONDARY_BOT",
        }
        adapter._team_bot_ids = {
            TEAM_ID: BOT_ID,
            OTHER_TEAM_ID: "B_SECONDARY",
        }
        adapter._fetch_thread_context = AsyncMock(return_value="thread context")

        await adapter._handle_slack_message(
            make_event("scoped request", team=""),
            payload={"team_id": OTHER_TEAM_ID},
        )

        primary_client.users_info.assert_not_awaited()
        primary_client.conversations_replies.assert_not_awaited()
        adapter._fetch_thread_context.assert_not_awaited()
        adapter.handle_message.assert_not_awaited()


class TestWorkspaceScopedReactionGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel_scope", ("missing", "ambiguous"))
    async def test_unresolved_reaction_is_suppressed_before_hook_or_lookup(
        self, adapter, channel_scope
    ):
        adapter.config.extra["reaction_triggers"] = ["thumbsup"]
        primary_client = AsyncMock()
        secondary_client = AsyncMock()
        primary_client.conversations_replies.return_value = {"messages": []}
        secondary_client.conversations_replies.return_value = {"messages": []}
        adapter._team_clients = {
            TEAM_ID: primary_client,
            OTHER_TEAM_ID: secondary_client,
        }
        adapter._team_bot_user_ids[OTHER_TEAM_ID] = "U_SECONDARY_BOT"
        adapter._reaction_handler = AsyncMock()
        adapter._handle_slack_message = AsyncMock()
        if channel_scope == "ambiguous":
            adapter._remember_channel_team(CHANNEL_ID, TEAM_ID)
            adapter._remember_channel_team(CHANNEL_ID, OTHER_TEAM_ID)
            assert CHANNEL_ID not in adapter._channel_team

        await adapter._handle_slack_reaction({
            "type": "reaction_added",
            "user": USER_ID,
            "reaction": "thumbsup",
            "item": {
                "type": "message",
                "channel": CHANNEL_ID,
                "ts": MESSAGE_TS,
            },
            "item_user": BOT_USER_ID,
            "event_ts": "1710000002.000003",
        })

        adapter._reaction_handler.assert_not_awaited()
        primary_client.conversations_replies.assert_not_awaited()
        secondary_client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_reaction_workspace_without_client_is_suppressed_before_hook(
        self, adapter
    ):
        adapter.config.extra["reaction_triggers"] = ["thumbsup"]
        primary_client = AsyncMock()
        adapter._team_clients = {TEAM_ID: primary_client}
        adapter._team_bot_user_ids = {
            TEAM_ID: BOT_USER_ID,
            OTHER_TEAM_ID: "U_SECONDARY_BOT",
        }
        adapter._reaction_handler = AsyncMock()
        adapter._handle_slack_message = AsyncMock()

        await adapter._handle_slack_reaction(
            {
                "type": "reaction_added",
                "user": USER_ID,
                "reaction": "thumbsup",
                "item": {
                    "type": "message",
                    "channel": CHANNEL_ID,
                    "ts": MESSAGE_TS,
                },
                "item_user": "U_SECONDARY_BOT",
                "event_ts": "1710000002.000003",
            },
            body={"team_id": OTHER_TEAM_ID},
        )

        adapter._reaction_handler.assert_not_awaited()
        primary_client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_body_only_reaction_scope_routes_with_explicit_workspace_client(
        self, adapter
    ):
        adapter.config.extra["reaction_triggers"] = ["thumbsup"]
        primary_client = AsyncMock()
        secondary_client = AsyncMock()
        primary_client.conversations_replies.return_value = {"messages": []}
        secondary_client.conversations_replies.return_value = {"messages": []}
        adapter._team_clients = {
            TEAM_ID: primary_client,
            OTHER_TEAM_ID: secondary_client,
        }
        adapter._team_bot_user_ids[OTHER_TEAM_ID] = "U_SECONDARY_BOT"
        adapter._handle_slack_message = AsyncMock()
        adapter._remember_channel_team(CHANNEL_ID, TEAM_ID)
        adapter._remember_channel_team(CHANNEL_ID, OTHER_TEAM_ID)
        assert CHANNEL_ID not in adapter._channel_team

        await adapter._handle_slack_reaction(
            {
                "type": "reaction_added",
                "user": USER_ID,
                "reaction": "thumbsup",
                "item": {
                    "type": "message",
                    "channel": CHANNEL_ID,
                    "ts": MESSAGE_TS,
                },
                "item_user": "U_SECONDARY_BOT",
                "event_ts": "1710000002.000003",
            },
            body={"team_id": OTHER_TEAM_ID},
        )

        primary_client.conversations_replies.assert_not_awaited()
        secondary_client.conversations_replies.assert_awaited_once()
        adapter._handle_slack_message.assert_awaited_once()
        reaction_call = adapter._handle_slack_message.await_args
        assert reaction_call is not None
        synthetic = reaction_call.args[0]
        assert synthetic["team"] == OTHER_TEAM_ID

    @pytest.mark.asyncio
    async def test_unambiguous_cached_reaction_scope_routes(self, adapter):
        adapter.config.extra["reaction_triggers"] = ["thumbsup"]
        primary_client = AsyncMock()
        primary_client.conversations_replies.return_value = {"messages": []}
        adapter._team_clients = {TEAM_ID: primary_client}
        adapter._team_bot_user_ids = {TEAM_ID: BOT_USER_ID}
        adapter._remember_channel_team(CHANNEL_ID, TEAM_ID)
        adapter._handle_slack_message = AsyncMock()

        await adapter._handle_slack_reaction({
            "type": "reaction_added",
            "user": USER_ID,
            "reaction": "thumbsup",
            "item": {
                "type": "message",
                "channel": CHANNEL_ID,
                "ts": MESSAGE_TS,
            },
            "item_user": BOT_USER_ID,
            "event_ts": "1710000002.000003",
        })

        primary_client.conversations_replies.assert_awaited_once()
        adapter._handle_slack_message.assert_awaited_once()
        reaction_call = adapter._handle_slack_message.await_args
        assert reaction_call is not None
        assert reaction_call.args[0]["team"] == TEAM_ID

    @pytest.mark.asyncio
    async def test_unique_registered_reaction_scope_routes_without_channel_cache(
        self, adapter
    ):
        adapter.config.extra["reaction_triggers"] = ["thumbsup"]
        primary_client = AsyncMock()
        primary_client.conversations_replies.return_value = {"messages": []}
        adapter._team_clients = {TEAM_ID: primary_client}
        adapter._team_bot_user_ids = {TEAM_ID: BOT_USER_ID}
        adapter._team_bot_ids = {TEAM_ID: BOT_ID}
        adapter._team_bot_names = {TEAM_ID: "hermes"}
        adapter._channel_team.clear()
        adapter._channel_teams.clear()
        adapter._reaction_handler = AsyncMock()
        adapter._handle_slack_message = AsyncMock()

        await adapter._handle_slack_reaction({
            "type": "reaction_added",
            "user": USER_ID,
            "reaction": "thumbsup",
            "item": {
                "type": "message",
                "channel": CHANNEL_ID,
                "ts": MESSAGE_TS,
            },
            "item_user": BOT_USER_ID,
            "event_ts": "1710000002.000003",
        })

        adapter._reaction_handler.assert_awaited_once()
        primary_client.conversations_replies.assert_awaited_once()
        adapter._handle_slack_message.assert_awaited_once()
        reaction_call = adapter._handle_slack_message.await_args
        assert reaction_call is not None
        assert reaction_call.args[0]["team"] == TEAM_ID

    @pytest.mark.asyncio
    async def test_legacy_teamless_reaction_routes_with_primary_client(self, adapter):
        adapter.config.extra["reaction_triggers"] = ["thumbsup"]
        primary_client = adapter._app.client
        primary_client.conversations_replies.return_value = {"messages": []}
        adapter._team_clients = {}
        adapter._team_bot_user_ids = {}
        adapter._team_bot_ids = {}
        adapter._team_bot_names = {}
        adapter._channel_team.clear()
        adapter._channel_teams.clear()
        adapter._reaction_handler = AsyncMock()
        adapter._handle_slack_message = AsyncMock()

        await adapter._handle_slack_reaction({
            "type": "reaction_added",
            "user": USER_ID,
            "reaction": "thumbsup",
            "item": {
                "type": "message",
                "channel": CHANNEL_ID,
                "ts": MESSAGE_TS,
            },
            "item_user": BOT_USER_ID,
            "event_ts": "1710000002.000003",
        })

        adapter._reaction_handler.assert_awaited_once()
        primary_client.conversations_replies.assert_awaited_once()
        adapter._handle_slack_message.assert_awaited_once()
        reaction_call = adapter._handle_slack_message.await_args
        assert reaction_call is not None
        assert "team" not in reaction_call.args[0]


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
    async def test_stop_with_configured_mention_pattern_sets_strict_and_suppresses(
        self, adapter, hermes_home
    ):
        adapter.config.extra["mention_patterns"] = [r"\bhermes\b"]

        await adapter._handle_slack_message(make_event("hermes stop responding"))

        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"
        adapter.handle_message.assert_not_awaited()

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

        # Unscoped/timestamp-only evidence cannot accept the control or wake a
        # scoped workspace. It is neither persisted nor sent to the agent.
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
    async def test_configured_mention_pattern_routes_without_clearing_strict(
        self, adapter, hermes_home
    ):
        adapter.config.extra["mention_patterns"] = [r"\bhermes\b"]
        write_strict_state(hermes_home)

        await adapter._handle_slack_message(make_event("hermes answer this"))

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

    @pytest.mark.asyncio
    async def test_pipe_form_bot_mention_routes_without_clearing_strict(
        self, adapter, hermes_home
    ):
        write_strict_state(hermes_home)

        await adapter._handle_slack_message(
            make_event(f"<@{BOT_USER_ID}|hermes> answer this")
        )

        adapter.handle_message.assert_awaited_once()
        assert read_state(hermes_home)[thread_key()]["mode"] == "strict_mention"

    @pytest.mark.asyncio
    async def test_strict_state_overrides_free_response_channel(self, adapter, hermes_home):
        adapter.config.extra["require_mention"] = False
        write_strict_state(hermes_home)

        await adapter._handle_slack_message(make_event("ordinary free-response turn"))

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

    @pytest.mark.asyncio
    async def test_resume_with_configured_mention_pattern_clears_strict_and_routes(
        self, adapter, hermes_home
    ):
        adapter.config.extra["mention_patterns"] = [r"\bhermes\b"]
        write_strict_state(hermes_home)

        await adapter._handle_slack_message(make_event("hermes unmute"))

        assert thread_key() not in read_state(hermes_home)
        adapter.handle_message.assert_awaited_once()


class TestOtherUserMentionGuard:
    @pytest.mark.asyncio
    async def test_primary_bot_id_collision_is_other_user_in_secondary_workspace(
        self, adapter
    ):
        primary_bot_uid = "U_PRIMARY"
        secondary_bot_uid = "U_SECONDARY"
        adapter._bot_user_id = primary_bot_uid
        adapter._team_bot_user_ids = {
            TEAM_ID: primary_bot_uid,
            OTHER_TEAM_ID: secondary_bot_uid,
        }
        adapter.config.extra.update({
            "ignore_other_user_mentions": True,
            "require_mention": False,
        })
        adapter._user_is_bot_cache[(OTHER_TEAM_ID, USER_ID)] = False
        event = make_event(
            f"<@{primary_bot_uid}> private request",
            team=OTHER_TEAM_ID,
        )
        event["client_msg_id"] = "client-secondary"

        await adapter._handle_slack_message(event)

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_user_mention_in_active_agent_thread_is_suppressed(
        self, adapter
    ):
        set_active_session(adapter)
        assert thread_key() not in adapter._slack_active_thread_keys

        await adapter._handle_slack_message(make_event(f"<@{OTHER_USER_ID}> посмотри пожалуйста"))

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pipe_form_other_user_mention_in_active_agent_thread_is_suppressed(
        self, adapter
    ):
        set_active_session(adapter)
        assert thread_key() not in adapter._slack_active_thread_keys

        await adapter._handle_slack_message(
            make_event(f"<@{OTHER_USER_ID}|alice> посмотри пожалуйста")
        )

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


    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ("/stop responding", "!stop responding"))
    async def test_pipe_form_bot_mention_routes_as_command(
        self, adapter, hermes_home, command
    ):
        set_active_session(adapter)

        await adapter._handle_slack_message(
            make_event(f"<@{BOT_USER_ID}|hermes> {command}")
        )

        assert read_state(hermes_home) == {}
        adapter.handle_message.assert_awaited_once()
        msg_event = adapter.handle_message.await_args.args[0]
        assert msg_event.text == "/stop responding"
        assert msg_event.message_type is MessageType.COMMAND


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
    async def test_corrupt_thread_routing_json_fails_closed_before_hydration(
        self, adapter, hermes_home
    ):
        path = state_file(hermes_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        adapter._fetch_thread_context = AsyncMock(return_value="thread context")

        await adapter._handle_slack_message(make_event(f"<@{BOT_USER_ID}> проверь"))

        adapter._fetch_thread_context.assert_not_awaited()
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_assistant_thread_lifecycle_does_not_bypass_strict_state(
        self, adapter, hermes_home
    ):
        await adapter._handle_assistant_thread_lifecycle_event({
            "type": "assistant_thread_started",
            "team_id": TEAM_ID,
            "assistant_thread": {
                "channel_id": CHANNEL_ID,
                "thread_ts": THREAD_TS,
                "user_id": USER_ID,
            },
        })
        assert (TEAM_ID, CHANNEL_ID, THREAD_TS) in adapter._assistant_threads
        write_strict_state(hermes_home)

        await adapter._handle_slack_message(
            make_event("unaddressed Assistant thread follow-up")
        )

        adapter.handle_message.assert_not_awaited()


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


class TestIsolatedPolicyIntegration:
    @pytest.mark.asyncio
    async def test_mention_pattern_evaluation_does_not_block_event_loop(
        self, adapter, monkeypatch
    ):
        entered = threading.Event()
        release = threading.Event()

        def contended_match(text):
            entered.set()
            release.wait(timeout=1.0)
            return False

        monkeypatch.setattr(
            adapter,
            "_slack_message_matches_mention_patterns",
            contended_match,
        )
        asyncio.get_running_loop().call_later(0.05, release.set)
        started = time.monotonic()

        await adapter._handle_slack_message(make_event("ordinary message"))

        assert entered.is_set()
        assert time.monotonic() - started < 0.5

    @pytest.mark.asyncio
    async def test_policy_evaluation_does_not_block_the_event_loop(self, adapter):
        entered = threading.Event()
        release = threading.Event()

        def contended_evaluate(context):
            entered.set()
            release.wait(timeout=1.0)
            return RoutingDecision(RoutingDisposition.DEFER)

        adapter._thread_routing_policy = MagicMock(enabled=True)
        adapter._thread_routing_policy.evaluate.side_effect = contended_evaluate
        asyncio.get_running_loop().call_later(0.05, release.set)
        started = time.monotonic()

        await adapter._handle_slack_message(make_event(f"<@{BOT_USER_ID}> check"))

        assert entered.is_set()
        assert time.monotonic() - started < 0.5

    @pytest.mark.asyncio
    async def test_policy_suppression_runs_before_attachment_hydration(self, adapter):
        adapter._thread_routing_policy = MagicMock()
        adapter._thread_routing_policy.evaluate.return_value = RoutingDecision(
            RoutingDisposition.SUPPRESS_STRICT,
            strict=True,
        )
        adapter._fetch_thread_context = AsyncMock(return_value="thread context")
        adapter._download_slack_file = AsyncMock(return_value="/tmp/downloaded.txt")

        await adapter._handle_slack_message(
            make_event(
                f"<@{BOT_USER_ID}> still suppressed",
                files=[
                    {
                        "id": "F1",
                        "name": "image.png",
                        "mimetype": "image/png",
                        "url_private_download": "https://files.slack.test/F1",
                    }
                ],
            )
        )

        adapter._thread_routing_policy.evaluate.assert_called_once()
        adapter._fetch_thread_context.assert_not_awaited()
        adapter._download_slack_file.assert_not_awaited()
        adapter.handle_message.assert_not_awaited()
