"""Behavior contracts for retiring Planner5D's legacy API model shim."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


ROUTE_ALIAS = "planner5d-route"
ROUTE_CONFIG = {
    "model": "openai/gpt-5.6",
    "provider": "openai-api",
    "base_url": "https://api.openai.com/v1",
}
LEGACY_CONFIG = {
    "model": "legacy/model",
    "provider": "legacy-provider",
}


def _adapter(extra: dict | None = None) -> APIServerAdapter:
    return APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={"model_name": "gateway-default", **(extra or {})},
        )
    )


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/models", adapter._handle_models)
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    return app


def _agent_result() -> tuple[dict, dict]:
    return (
        {"final_response": "ok", "messages": [], "api_calls": 1},
        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


def _run_agent_stub() -> MagicMock:
    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": "done"}
    agent.session_prompt_tokens = 1
    agent.session_completion_tokens = 1
    agent.session_total_tokens = 2
    return agent


@pytest.mark.asyncio
async def test_legacy_models_config_is_ignored_and_not_advertised():
    adapter = _adapter({"models": {"legacy-alias": LEGACY_CONFIG}})

    assert adapter._model_routes == {}

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.get("/v1/models")
        assert response.status == 200
        payload = await response.json()

    assert [model["id"] for model in payload["data"]] == ["gateway-default"]
    assert payload["data"][0]["root"] == "gateway-default"
    assert payload["data"][0]["parent"] is None


def test_model_routes_ignore_retired_api_key_env_and_api_mode():
    adapter = _adapter({
        "model_routes": {
            ROUTE_ALIAS: {
                **ROUTE_CONFIG,
                "api_key_env": "OPENAI_API_KEY",
                "api_mode": "responses",
            }
        }
    })

    assert adapter._model_routes == {ROUTE_ALIAS: ROUTE_CONFIG}


@pytest.mark.asyncio
async def test_legacy_alias_does_not_route_or_inject_prompt_metadata():
    adapter = _adapter({"models": {"legacy-alias": LEGACY_CONFIG}})

    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as run_agent:
        run_agent.return_value = _agent_result()
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "legacy-alias",
                    "messages": [
                        {"role": "system", "content": "Keep the original prompt."},
                        {"role": "user", "content": "hello"},
                    ],
                },
            )

    assert response.status == 200
    assert run_agent.call_args.kwargs["route"] is None
    assert (
        run_agent.call_args.kwargs["ephemeral_system_prompt"]
        == "Keep the original prompt."
    )


@pytest.mark.asyncio
async def test_canonical_model_route_drives_chat_responses_and_runs_without_prompt_injection():
    adapter = _adapter({"model_routes": {ROUTE_ALIAS: ROUTE_CONFIG}})
    run_agent = AsyncMock(return_value=_agent_result())
    create_agent = MagicMock(return_value=_run_agent_stub())

    with (
        patch.object(adapter, "_run_agent", run_agent),
        patch.object(adapter, "_create_agent", create_agent),
    ):
        async with TestClient(TestServer(_app(adapter))) as client:
            chat_response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": ROUTE_ALIAS,
                    "messages": [
                        {"role": "system", "content": "Chat instructions."},
                        {"role": "user", "content": "hello"},
                    ],
                },
            )
            responses_response = await client.post(
                "/v1/responses",
                json={
                    "model": ROUTE_ALIAS,
                    "instructions": "Responses instructions.",
                    "input": "hello",
                    "store": False,
                },
            )
            runs_response = await client.post(
                "/v1/runs",
                json={
                    "model": ROUTE_ALIAS,
                    "instructions": "Runs instructions.",
                    "input": "hello",
                },
            )
            run_payload = await runs_response.json()
            for _ in range(20):
                if create_agent.call_args is not None:
                    break
                await asyncio.sleep(0.01)
            status_response = await client.get(f"/v1/runs/{run_payload['run_id']}")

    assert chat_response.status == 200
    assert responses_response.status == 200
    assert runs_response.status == 202
    assert status_response.status == 200

    chat_call, responses_call = run_agent.call_args_list
    assert chat_call.kwargs["route"] == ROUTE_CONFIG
    assert chat_call.kwargs["ephemeral_system_prompt"] == "Chat instructions."
    assert responses_call.kwargs["route"] == ROUTE_CONFIG
    assert responses_call.kwargs["ephemeral_system_prompt"] == "Responses instructions."
    assert create_agent.call_args is not None
    assert create_agent.call_args.kwargs["route"] == ROUTE_CONFIG
    assert (
        create_agent.call_args.kwargs["ephemeral_system_prompt"] == "Runs instructions."
    )


@pytest.mark.asyncio
async def test_omitted_model_keeps_default_across_chat_responses_and_runs():
    adapter = _adapter({"model_routes": {ROUTE_ALIAS: ROUTE_CONFIG}})
    run_agent = AsyncMock(return_value=_agent_result())
    create_agent = MagicMock(return_value=_run_agent_stub())

    with (
        patch.object(adapter, "_run_agent", run_agent),
        patch.object(adapter, "_create_agent", create_agent),
    ):
        async with TestClient(TestServer(_app(adapter))) as client:
            chat_response = await client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hello"}]},
            )
            responses_response = await client.post(
                "/v1/responses",
                json={"input": "hello", "store": False},
            )
            runs_response = await client.post("/v1/runs", json={"input": "hello"})
            run_payload = await runs_response.json()
            for _ in range(20):
                if create_agent.call_args is not None:
                    break
                await asyncio.sleep(0.01)
            status_response = await client.get(f"/v1/runs/{run_payload['run_id']}")

    assert chat_response.status == 200
    assert responses_response.status == 200
    assert runs_response.status == 202
    assert status_response.status == 200
    assert [call.kwargs["route"] for call in run_agent.call_args_list] == [None, None]
    assert create_agent.call_args is not None
    assert create_agent.call_args.kwargs["route"] is None
