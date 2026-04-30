"""Tests for API-server model alias routing.

The OpenAI-compatible API server advertises model IDs to frontends like
Open WebUI. When configured with a `models` map, a request's `model` field
should select the backing provider/model for that request only.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    cors_middleware,
    security_headers_middleware,
)


def _adapter_with_models() -> APIServerAdapter:
    config = PlatformConfig(
        enabled=True,
        extra={
            "models": {
                "hermes-dev-gpt": {
                    "provider": "openai",
                    "model": "gpt-5.5",
                    "api_key_env": "OPENAI_API_KEY",
                },
                "hermes-dev-gemini": {
                    "provider": "gemini",
                    "model": "gemini-3-flash-preview",
                    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                    "api_key_env": "GOOGLE_API_KEY",
                },
            }
        },
    )
    return APIServerAdapter(config)


def _create_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_get("/v1/models", adapter._handle_models)
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    return app


@pytest.mark.asyncio
async def test_models_endpoint_lists_configured_aliases():
    adapter = _adapter_with_models()
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/v1/models")
        assert resp.status == 200
        data = await resp.json()

    assert data["object"] == "list"
    assert [item["id"] for item in data["data"]] == [
        "hermes-dev-gemini",
        "hermes-dev-gpt",
    ]
    assert [item["root"] for item in data["data"]] == [
        "hermes-dev-gemini",
        "hermes-dev-gpt",
    ]


@pytest.mark.asyncio
async def test_chat_completion_routes_requested_alias_to_backing_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    adapter = _adapter_with_models()
    app = _create_app(adapter)

    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (
            {"final_response": "ok", "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={
                    "model": "hermes-dev-gpt",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

    assert resp.status == 200
    assert mock_run.call_args.kwargs["model_override"] == {
        "alias": "hermes-dev-gpt",
        "model": "gpt-5.5",
        "provider": "openai",
        "api_key": "sk-openai",
        "base_url": None,
        "api_mode": None,
    }


@pytest.mark.asyncio
async def test_chat_completion_appends_routed_model_metadata_to_system_prompt(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    adapter = _adapter_with_models()
    app = _create_app(adapter)

    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (
            {"final_response": "ok", "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={
                    "model": "hermes-dev-gpt",
                    "messages": [
                        {"role": "system", "content": "You are concise."},
                        {"role": "user", "content": "hello"},
                    ],
                },
            )

    assert resp.status == 200
    prompt = mock_run.call_args.kwargs["ephemeral_system_prompt"]
    assert "You are concise." in prompt
    assert "Runtime model routing metadata" in prompt
    assert "current requested alias is `hermes-dev-gpt`" in prompt
    assert "backing model is `gpt-5.5`" in prompt


@pytest.mark.asyncio
async def test_responses_routes_requested_alias_to_backing_model(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-google")
    adapter = _adapter_with_models()
    app = _create_app(adapter)

    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (
            {"final_response": "ok", "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/responses",
                json={
                    "model": "hermes-dev-gemini",
                    "input": "hello",
                    "store": False,
                },
            )

    assert resp.status == 200
    assert mock_run.call_args.kwargs["model_override"] == {
        "alias": "hermes-dev-gemini",
        "model": "gemini-3-flash-preview",
        "provider": "gemini",
        "api_key": "sk-google",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_mode": None,
    }


@pytest.mark.asyncio
async def test_chat_completion_streaming_routes_requested_alias(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    adapter = _adapter_with_models()
    app = _create_app(adapter)

    async def _mock_run_agent(**kwargs):
        return (
            {"final_response": "ok", "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    with (
        patch.object(adapter, "_run_agent", side_effect=_mock_run_agent) as mock_run,
        patch.object(adapter, "_write_sse_chat_completion", new_callable=AsyncMock) as mock_sse,
    ):
        mock_sse.return_value = web.json_response({"ok": True})
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={
                    "model": "hermes-dev-gpt",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            await asyncio.sleep(0)

    assert resp.status == 200
    assert mock_run.call_args.kwargs["model_override"]["model"] == "gpt-5.5"
    assert mock_run.call_args.kwargs["model_override"]["api_key"] == "sk-openai"
    prompt = mock_run.call_args.kwargs["ephemeral_system_prompt"]
    assert "current requested alias is `hermes-dev-gpt`" in prompt
    assert "backing model is `gpt-5.5`" in prompt


@pytest.mark.asyncio
async def test_responses_streaming_routes_requested_alias(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-google")
    adapter = _adapter_with_models()
    app = _create_app(adapter)

    async def _mock_run_agent(**kwargs):
        return (
            {"final_response": "ok", "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    with (
        patch.object(adapter, "_run_agent", side_effect=_mock_run_agent) as mock_run,
        patch.object(adapter, "_write_sse_responses", new_callable=AsyncMock) as mock_sse,
    ):
        mock_sse.return_value = web.json_response({"ok": True})
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/responses",
                json={
                    "model": "hermes-dev-gemini",
                    "input": "hello",
                    "stream": True,
                    "store": False,
                },
            )
            await asyncio.sleep(0)

    assert resp.status == 200
    assert mock_run.call_args.kwargs["model_override"]["model"] == "gemini-3-flash-preview"
    assert mock_run.call_args.kwargs["model_override"]["api_key"] == "sk-google"
    prompt = mock_run.call_args.kwargs["ephemeral_system_prompt"]
    assert "current requested alias is `hermes-dev-gemini`" in prompt
    assert "backing model is `gemini-3-flash-preview`" in prompt


@pytest.mark.asyncio
async def test_responses_appends_routed_model_metadata_to_instructions(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-google")
    adapter = _adapter_with_models()
    app = _create_app(adapter)

    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (
            {"final_response": "ok", "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/responses",
                json={
                    "model": "hermes-dev-gemini",
                    "instructions": "Answer tersely.",
                    "input": "hello",
                    "store": False,
                },
            )

    assert resp.status == 200
    prompt = mock_run.call_args.kwargs["ephemeral_system_prompt"]
    assert "Answer tersely." in prompt
    assert "current requested alias is `hermes-dev-gemini`" in prompt
    assert "backing model is `gemini-3-flash-preview`" in prompt


@pytest.mark.asyncio
async def test_runs_routes_requested_alias(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    adapter = _adapter_with_models()
    app = _create_app(adapter)

    mock_agent = MagicMock()
    mock_agent.run_conversation.return_value = {"final_response": "done"}
    mock_agent.session_prompt_tokens = 1
    mock_agent.session_completion_tokens = 1
    mock_agent.session_total_tokens = 2

    with patch.object(adapter, "_create_agent", return_value=mock_agent) as mock_create:
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/runs",
                json={"model": "hermes-dev-gpt", "input": "hello"},
            )
            assert resp.status == 202
            data = await resp.json()

            for _ in range(20):
                if mock_create.call_args is not None:
                    break
                await asyncio.sleep(0.01)

            status_resp = await cli.get(f"/v1/runs/{data['run_id']}")
            assert status_resp.status == 200
            status = await status_resp.json()

    assert mock_create.call_args.kwargs["model_override"]["model"] == "gpt-5.5"
    assert mock_create.call_args.kwargs["model_override"]["api_key"] == "sk-openai"
    prompt = mock_create.call_args.kwargs["ephemeral_system_prompt"]
    assert "current requested alias is `hermes-dev-gpt`" in prompt
    assert "backing model is `gpt-5.5`" in prompt
    assert status["model"] == "hermes-dev-gpt"


@pytest.mark.asyncio
async def test_unknown_model_alias_returns_400():
    adapter = _adapter_with_models()
    app = _create_app(adapter)

    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (
            {"final_response": "ok", "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={
                    "model": "missing-model",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            assert resp.status == 400
            data = await resp.json()

    assert data["error"]["code"] == "model_not_found"
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_non_string_model_returns_400():
    adapter = _adapter_with_models()
    app = _create_app(adapter)

    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={
                    "model": {"name": "hermes-dev-gpt"},
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            assert resp.status == 400
            data = await resp.json()

    assert data["error"]["code"] == "invalid_model"
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_missing_api_key_env_returns_model_misconfigured(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    adapter = _adapter_with_models()
    app = _create_app(adapter)

    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={
                    "model": "hermes-dev-gpt",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            assert resp.status == 503
            data = await resp.json()

    assert data["error"]["code"] == "model_misconfigured"
    mock_run.assert_not_called()


def test_create_agent_applies_model_override_without_primary_runtime_leaks(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    adapter = _adapter_with_models()
    primary_pool = object()
    agent_instance = MagicMock()

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
            "provider": "gemini",
            "api_key": "sk-google",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "api_mode": None,
            "command": "gemini-cli",
            "args": ["--project", "planner-5d"],
            "credential_pool": primary_pool,
        }),
        patch("gateway.run._resolve_gateway_model", return_value="gemini-3-flash-preview"),
        patch("gateway.run._load_gateway_config", return_value={}),
        patch("gateway.run.GatewayRunner._load_fallback_model", return_value=None),
        patch("hermes_cli.tools_config._get_platform_tools", return_value=set()),
        patch.object(adapter, "_ensure_session_db", return_value=None),
        patch("run_agent.AIAgent", return_value=agent_instance) as mock_agent_cls,
    ):
        agent = adapter._create_agent(
            session_id="session-1",
            model_override=adapter._resolve_model_override("hermes-dev-gpt"),
        )

    assert agent is agent_instance
    kwargs = mock_agent_cls.call_args.kwargs
    assert kwargs["model"] == "gpt-5.5"
    assert kwargs["provider"] == "openai"
    assert kwargs["api_key"] == "sk-openai"
    assert kwargs["base_url"] is None
    assert kwargs["api_mode"] is None
    assert kwargs["credential_pool"] is None
    assert kwargs["command"] is None
    assert kwargs["args"] == []


def test_routed_model_metadata_not_added_for_single_model_mode():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"model_name": "dev"}))

    assert adapter._append_routed_model_metadata("Existing prompt", None) == "Existing prompt"


@pytest.mark.asyncio
async def test_missing_models_map_keeps_single_model_behavior():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"model_name": "dev"}))
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/v1/models")
        assert resp.status == 200
        data = await resp.json()

    assert [item["id"] for item in data["data"]] == ["dev"]


@pytest.mark.asyncio
async def test_capabilities_lists_configured_model_aliases():
    adapter = _adapter_with_models()
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/v1/capabilities")
        assert resp.status == 200
        data = await resp.json()

    assert data["models"] == ["hermes-dev-gemini", "hermes-dev-gpt"]


def test_models_config_with_no_valid_entries_fails_startup():
    config = PlatformConfig(
        enabled=True,
        extra={
            "models": {
                "broken": {"provider": "openai"},
                " ": {"model": "gpt-5.5"},
            }
        },
    )

    with pytest.raises(ValueError, match="platforms.api_server.models"):
        APIServerAdapter(config)


def test_models_config_requires_mapping_shape():
    config = PlatformConfig(enabled=True, extra={"models": []})

    with pytest.raises(ValueError, match="must be a mapping, got list"):
        APIServerAdapter(config)
