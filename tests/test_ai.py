"""Tests for user-level AI integration (Issue 20).

Covers:
  - GET/POST/DELETE /api/ai/config
  - POST /api/ai/chat  (Claude server-proxy path)
  - POST /api/ai/inline (Claude server-proxy path)
  - Ollama rejection (browser-direct path)
  - _get_user_ai_config fallback logic
  - New settings defaults (inlineSystemPrompt / chatSystemPrompt)
  - ai_enabled flag on /editor
"""
import json
import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database import decrypt_token, encrypt_token
from app.models import UserAIConfig


# ---------------------------------------------------------------------------
# Auth guards
# ---------------------------------------------------------------------------

async def test_get_ai_config_unauthenticated(anon_client):
    resp = await anon_client.get("/api/ai/config")
    assert resp.status_code == 401


async def test_post_ai_config_unauthenticated(anon_client):
    resp = await anon_client.post("/api/ai/config", json={"provider": "claude"})
    assert resp.status_code == 401


async def test_delete_ai_config_unauthenticated(anon_client):
    resp = await anon_client.delete("/api/ai/config")
    assert resp.status_code == 401


async def test_ai_chat_unauthenticated(anon_client):
    resp = await anon_client.post("/api/ai/chat", json={"message": "hello"})
    assert resp.status_code == 401


async def test_ai_inline_unauthenticated(anon_client):
    resp = await anon_client.post("/api/ai/inline", json={"prompt": "rewrite"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/ai/config — no row in DB
# ---------------------------------------------------------------------------

async def test_get_ai_config_defaults_when_no_row(auth_client, monkeypatch):
    """Without a user_ai_config row, returns defaults derived from server env."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "")
    monkeypatch.setattr(proxy_mod, "_AI_MODEL", "claude-sonnet-4-6")

    resp = await auth_client.get("/api/ai/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "claude"
    assert data["has_key"] is False
    assert data["model"] == "claude-sonnet-4-6"
    assert data["ollama_endpoint"] == ""
    assert data["server_key_active"] is False


async def test_get_ai_config_reflects_server_key(auth_client, monkeypatch):
    """When server ANTHROPIC_API_KEY is set and no user row, server_key_active is True."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "sk-server-key")

    resp = await auth_client.get("/api/ai/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_key"] is True
    assert data["server_key_active"] is True


# ---------------------------------------------------------------------------
# POST /api/ai/config
# ---------------------------------------------------------------------------

async def test_post_ai_config_saves_claude_key(auth_client, test_user, db_session):
    """Saving a Claude key encrypts it and stores a user_ai_config row."""
    resp = await auth_client.post(
        "/api/ai/config",
        json={"provider": "claude", "api_key": "sk-ant-test123", "model": "claude-sonnet-4-6"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    from sqlalchemy import select
    result = await db_session.execute(
        select(UserAIConfig).where(UserAIConfig.user_id == test_user.id)
    )
    cfg = result.scalar_one_or_none()
    assert cfg is not None
    assert cfg.provider == "claude"
    assert cfg.model == "claude-sonnet-4-6"
    # Key must be stored encrypted, not in plaintext
    assert cfg.api_key_encrypted is not None
    assert cfg.api_key_encrypted != b"sk-ant-test123"
    assert decrypt_token(cfg.api_key_encrypted) == "sk-ant-test123"


async def test_post_ai_config_saves_ollama(auth_client, test_user, db_session):
    """Saving Ollama config stores endpoint and model; no key is encrypted."""
    resp = await auth_client.post(
        "/api/ai/config",
        json={
            "provider": "ollama",
            "model": "llama3",
            "ollama_endpoint": "http://localhost:11434",
        },
    )
    assert resp.status_code == 200

    from sqlalchemy import select
    result = await db_session.execute(
        select(UserAIConfig).where(UserAIConfig.user_id == test_user.id)
    )
    cfg = result.scalar_one_or_none()
    assert cfg.provider == "ollama"
    assert cfg.model == "llama3"
    assert cfg.ollama_endpoint == "http://localhost:11434"
    assert cfg.api_key_encrypted is None


async def test_post_ai_config_upserts_existing_row(auth_client, test_user, db_session):
    """Saving config twice updates the existing row rather than creating a duplicate."""
    await auth_client.post(
        "/api/ai/config",
        json={"provider": "claude", "api_key": "sk-ant-first"},
    )
    await auth_client.post(
        "/api/ai/config",
        json={"provider": "claude", "api_key": "sk-ant-second"},
    )

    from sqlalchemy import select
    result = await db_session.execute(
        select(UserAIConfig).where(UserAIConfig.user_id == test_user.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert decrypt_token(rows[0].api_key_encrypted) == "sk-ant-second"


async def test_post_ai_config_rejects_invalid_provider(auth_client):
    """An unknown provider value must be rejected with 400."""
    resp = await auth_client.post(
        "/api/ai/config",
        json={"provider": "openai", "api_key": "sk-oai-xyz"},
    )
    assert resp.status_code == 400


async def test_post_ai_config_without_key_does_not_overwrite_existing_key(
    auth_client, test_user, db_session
):
    """POSTing config without api_key must NOT clear an already-stored key."""
    # Seed a row with an encrypted key
    encrypted = encrypt_token("sk-ant-existing")
    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="claude",
        api_key_encrypted=encrypted,
        model="claude-opus-4-8",
    )
    db_session.add(cfg)
    await db_session.commit()

    # Update model only — no api_key in payload
    resp = await auth_client.post(
        "/api/ai/config",
        json={"provider": "claude", "model": "claude-sonnet-4-6"},
    )
    assert resp.status_code == 200

    from sqlalchemy import select
    await db_session.refresh(cfg)
    result = await db_session.execute(
        select(UserAIConfig).where(UserAIConfig.user_id == test_user.id)
    )
    updated = result.scalar_one()
    # Key must still be the original
    assert updated.api_key_encrypted == encrypted
    assert updated.model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# GET /api/ai/config — after saving
# ---------------------------------------------------------------------------

async def test_get_ai_config_after_save(auth_client):
    """GET reflects what was saved via POST."""
    await auth_client.post(
        "/api/ai/config",
        json={"provider": "ollama", "model": "mistral", "ollama_endpoint": "http://gpu-box:11434"},
    )
    resp = await auth_client.get("/api/ai/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "ollama"
    assert data["model"] == "mistral"
    assert data["ollama_endpoint"] == "http://gpu-box:11434"
    assert data["has_key"] is True   # Ollama = always "has key" (no key needed)
    # The actual key is never returned
    assert "api_key" not in data
    assert "api_key_encrypted" not in data


async def test_get_ai_config_has_key_true_for_claude_with_stored_key(
    auth_client, test_user, db_session
):
    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="claude",
        api_key_encrypted=encrypt_token("sk-ant-abc"),
    )
    db_session.add(cfg)
    await db_session.commit()

    resp = await auth_client.get("/api/ai/config")
    data = resp.json()
    assert data["has_key"] is True
    assert data["server_key_active"] is False


# ---------------------------------------------------------------------------
# DELETE /api/ai/config
# ---------------------------------------------------------------------------

async def test_delete_ai_config_clears_key(auth_client, test_user, db_session):
    """DELETE removes the stored API key but leaves the provider/model row intact."""
    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="claude",
        api_key_encrypted=encrypt_token("sk-ant-todelete"),
        model="claude-sonnet-4-6",
    )
    db_session.add(cfg)
    await db_session.commit()

    resp = await auth_client.delete("/api/ai/config")
    assert resp.status_code == 200

    # The endpoint used a separate session; refresh our object from the DB.
    await db_session.refresh(cfg)
    assert cfg.api_key_encrypted is None

    # GET must reflect has_key = False
    get_resp = await auth_client.get("/api/ai/config")
    assert get_resp.json()["has_key"] is False


async def test_delete_ai_config_no_row_is_noop(auth_client):
    """DELETE with no existing row must return 200 (idempotent)."""
    resp = await auth_client.delete("/api/ai/config")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/ai/chat — Claude path
# ---------------------------------------------------------------------------

async def _fake_anthropic_stream():
    """Yields Anthropic SSE bytes simulating a two-chunk response."""
    chunks = [
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}\n\n',
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":" world"}}\n\n',
    ]
    for chunk in chunks:
        yield chunk


async def test_ai_chat_returns_503_when_no_key(auth_client, monkeypatch):
    """Without a key (no server key, no user key), /api/ai/chat returns 503."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "")

    resp = await auth_client.post("/api/ai/chat", json={"message": "hello"})
    assert resp.status_code == 503


async def test_ai_chat_uses_user_key(auth_client, test_user, db_session, monkeypatch):
    """When user has a stored Claude key, /api/ai/chat uses it (not the server env)."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "")   # no server key

    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="claude",
        api_key_encrypted=encrypt_token("sk-ant-user"),
    )
    db_session.add(cfg)
    await db_session.commit()

    captured = {}

    class FakeResponse:
        status_code = 200
        async def aiter_text(self):
            yield 'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}\n\n'
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        def stream(self, method, url, headers, json):
            captured["key"] = headers.get("x-api-key")
            return FakeResponse()

    with patch("app.proxy.httpx.AsyncClient", return_value=FakeClient()):
        resp = await auth_client.post("/api/ai/chat", json={"message": "hello"})

    assert resp.status_code == 200
    assert captured["key"] == "sk-ant-user"


async def test_ai_chat_passes_custom_system_prompt(auth_client, test_user, db_session, monkeypatch):
    """system_prompt from the request body is forwarded to Anthropic."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "sk-server")

    captured = {}

    class FakeResponse:
        status_code = 200
        async def aiter_text(self):
            yield ""
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        def stream(self, method, url, headers, json):
            captured["system"] = json.get("system")
            return FakeResponse()

    custom_prompt = "You are a pirate. Respond only in pirate speak."
    with patch("app.proxy.httpx.AsyncClient", return_value=FakeClient()):
        resp = await auth_client.post(
            "/api/ai/chat",
            json={"message": "hello", "system_prompt": custom_prompt},
        )

    assert resp.status_code == 200
    assert captured["system"] == custom_prompt


async def test_ai_chat_uses_default_system_prompt_when_none_provided(
    auth_client, monkeypatch
):
    """When no system_prompt is in the request, the default chatSystemPrompt is used."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "sk-server")

    captured = {}

    class FakeResponse:
        status_code = 200
        async def aiter_text(self):
            yield ""
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        def stream(self, method, url, headers, json):
            captured["system"] = json.get("system")
            return FakeResponse()

    with patch("app.proxy.httpx.AsyncClient", return_value=FakeClient()):
        await auth_client.post("/api/ai/chat", json={"message": "hello"})

    assert proxy_mod._SETTINGS_DEFAULTS["chatSystemPrompt"] in captured["system"]


async def test_ai_chat_rejects_ollama_provider(auth_client, test_user, db_session):
    """When user's provider is 'ollama', /api/ai/chat returns 400 with detail 'ollama_browser_direct'."""
    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="ollama",
        ollama_endpoint="http://localhost:11434",
    )
    db_session.add(cfg)
    await db_session.commit()

    resp = await auth_client.post("/api/ai/chat", json={"message": "hello"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "ollama_browser_direct"


# ---------------------------------------------------------------------------
# POST /api/ai/inline — Claude path
# ---------------------------------------------------------------------------

async def test_ai_inline_returns_503_when_no_key(auth_client, monkeypatch):
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "")

    resp = await auth_client.post("/api/ai/inline", json={"prompt": "rewrite this"})
    assert resp.status_code == 503


async def test_ai_inline_returns_text(auth_client, test_user, db_session, monkeypatch):
    """Successful inline call returns {"text": "..."}."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "")

    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="claude",
        api_key_encrypted=encrypt_token("sk-ant-inline"),
    )
    db_session.add(cfg)
    await db_session.commit()

    fake_response_body = {
        "content": [{"type": "text", "text": "Rewritten text here."}]
    }

    class FakeResponse:
        status_code = 200
        def json(self): return fake_response_body

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=FakeResponse())

    with patch("app.proxy.httpx.AsyncClient", return_value=mock_client):
        resp = await auth_client.post(
            "/api/ai/inline",
            json={"prompt": "make this shorter", "selection": "A very long sentence."},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["text"] == "Rewritten text here."


async def test_ai_inline_passes_selection_in_user_content(
    auth_client, test_user, db_session, monkeypatch
):
    """The selection text is wrapped in <selection> tags in the user message."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "")

    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="claude",
        api_key_encrypted=encrypt_token("sk-ant-test"),
    )
    db_session.add(cfg)
    await db_session.commit()

    captured = {}

    class FakeResponse:
        status_code = 200
        def json(self): return {"content": [{"type": "text", "text": "ok"}]}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    async def fake_post(url, headers, json):
        captured["messages"] = json.get("messages", [])
        return FakeResponse()

    mock_client.post = fake_post

    with patch("app.proxy.httpx.AsyncClient", return_value=mock_client):
        await auth_client.post(
            "/api/ai/inline",
            json={"prompt": "rewrite", "selection": "original text"},
        )

    user_msg = captured["messages"][0]["content"]
    assert "<selection>" in user_msg
    assert "original text" in user_msg


async def test_ai_inline_uses_custom_system_prompt(
    auth_client, test_user, db_session, monkeypatch
):
    """system_prompt in the request body is forwarded to Anthropic."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "sk-server")

    captured = {}

    class FakeResponse:
        status_code = 200
        def json(self): return {"content": [{"type": "text", "text": "done"}]}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    async def fake_post(url, headers, json):
        captured["system"] = json.get("system")
        return FakeResponse()

    mock_client.post = fake_post

    with patch("app.proxy.httpx.AsyncClient", return_value=mock_client):
        await auth_client.post(
            "/api/ai/inline",
            json={"prompt": "rewrite", "system_prompt": "Be extremely terse."},
        )

    assert captured["system"] == "Be extremely terse."


async def test_ai_inline_rejects_ollama_provider(auth_client, test_user, db_session):
    """When provider is 'ollama', /api/ai/inline returns 400 ollama_browser_direct."""
    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="ollama",
        ollama_endpoint="http://localhost:11434",
    )
    db_session.add(cfg)
    await db_session.commit()

    resp = await auth_client.post("/api/ai/inline", json={"prompt": "rewrite"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "ollama_browser_direct"


async def test_ai_inline_without_selection(auth_client, monkeypatch):
    """When no selection is provided, the prompt is sent as-is without <selection> tags."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "sk-server")

    captured = {}

    class FakeResponse:
        status_code = 200
        def json(self): return {"content": [{"type": "text", "text": "answer"}]}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    async def fake_post(url, headers, json):
        captured["messages"] = json.get("messages", [])
        return FakeResponse()

    mock_client.post = fake_post

    with patch("app.proxy.httpx.AsyncClient", return_value=mock_client):
        resp = await auth_client.post("/api/ai/inline", json={"prompt": "what is Quarto?"})

    assert resp.status_code == 200
    user_msg = captured["messages"][0]["content"]
    assert "<selection>" not in user_msg
    assert "what is Quarto?" in user_msg


# ---------------------------------------------------------------------------
# _get_user_ai_config fallback logic
# ---------------------------------------------------------------------------

async def test_get_user_ai_config_falls_back_to_server_env(test_user, monkeypatch):
    """When no user_ai_config row exists, falls back to server env vars."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "sk-server-fallback")
    monkeypatch.setattr(proxy_mod, "_AI_MODEL", "claude-sonnet-4-6")

    provider, api_key, model, ollama_endpoint = await proxy_mod._get_user_ai_config(
        test_user.id
    )
    assert provider == "claude"
    assert api_key == "sk-server-fallback"
    assert model == "claude-sonnet-4-6"
    assert ollama_endpoint == ""


async def test_get_user_ai_config_uses_user_row(test_user, db_session, monkeypatch):
    """When a user row exists with a key, it is decrypted and returned."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "sk-server-should-not-be-used")

    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="claude",
        api_key_encrypted=encrypt_token("sk-ant-user-key"),
        model="claude-opus-4-8",
    )
    db_session.add(cfg)
    await db_session.commit()

    provider, api_key, model, _ = await proxy_mod._get_user_ai_config(test_user.id)
    assert provider == "claude"
    assert api_key == "sk-ant-user-key"
    assert model == "claude-opus-4-8"


async def test_get_user_ai_config_ollama_returns_empty_key(test_user, db_session):
    """For Ollama provider, api_key is always empty (calls are browser-direct)."""
    import app.proxy as proxy_mod

    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="ollama",
        ollama_endpoint="http://localhost:11434",
        model="llama3",
    )
    db_session.add(cfg)
    await db_session.commit()

    provider, api_key, model, ollama_endpoint = await proxy_mod._get_user_ai_config(
        test_user.id
    )
    assert provider == "ollama"
    assert api_key == ""
    assert model == "llama3"
    assert ollama_endpoint == "http://localhost:11434"


async def test_get_user_ai_config_claude_no_user_key_falls_back_to_server(
    test_user, db_session, monkeypatch
):
    """Claude row with no stored key falls back to server env var key."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "sk-server-backup")

    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="claude",
        api_key_encrypted=None,  # no key stored
        model="claude-sonnet-4-6",
    )
    db_session.add(cfg)
    await db_session.commit()

    _, api_key, _, _ = await proxy_mod._get_user_ai_config(test_user.id)
    assert api_key == "sk-server-backup"


async def test_get_user_ai_config_decrypt_failure_falls_back_to_server(
    test_user, db_session, monkeypatch
):
    """If stored key is undecryptable (e.g. after key rotation), falls back to server key."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "sk-server-fallback")

    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="claude",
        api_key_encrypted=b"invalid-ciphertext",
    )
    db_session.add(cfg)
    await db_session.commit()

    _, api_key, _, _ = await proxy_mod._get_user_ai_config(test_user.id)
    assert api_key == "sk-server-fallback"


async def test_save_ai_config_switching_to_ollama_clears_claude_key(
    auth_client, test_user, db_session, monkeypatch
):
    """Switching provider from claude to ollama must clear the stored API key."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "")

    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="claude",
        api_key_encrypted=encrypt_token("sk-ant-secret"),
    )
    db_session.add(cfg)
    await db_session.commit()

    resp = await auth_client.post(
        "/api/ai/config",
        json={"provider": "ollama", "ollama_endpoint": "http://localhost:11434"},
    )
    assert resp.status_code == 200

    await db_session.refresh(cfg)
    assert cfg.api_key_encrypted is None


async def test_ai_inline_empty_content_list_returns_empty_text(
    auth_client, monkeypatch
):
    """Anthropic returning empty content list must return '' not crash with IndexError."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "sk-server")

    class FakeResponse:
        status_code = 200
        def json(self): return {"content": [], "stop_reason": "end_turn"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=FakeResponse())

    with patch("app.proxy.httpx.AsyncClient", return_value=mock_client):
        resp = await auth_client.post("/api/ai/inline", json={"prompt": "rewrite"})

    assert resp.status_code == 200
    assert resp.json()["text"] == ""


# ---------------------------------------------------------------------------
# New settings defaults include prompt fields
# ---------------------------------------------------------------------------

async def test_settings_defaults_include_prompt_fields(auth_client, monkeypatch):
    """GET /api/settings returns inlineSystemPrompt and chatSystemPrompt in defaults."""
    from app import proxy

    async def _mock_ensure_cloned(_user_id, _username, _token):
        return False

    monkeypatch.setattr(proxy, "_ensure_settings_cloned", _mock_ensure_cloned)

    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 200
    settings = resp.json()["settings"]
    assert "inlineSystemPrompt" in settings
    assert "chatSystemPrompt" in settings
    assert len(settings["inlineSystemPrompt"]) > 0
    assert len(settings["chatSystemPrompt"]) > 0


async def test_custom_system_prompts_are_saved_and_returned(
    auth_client, monkeypatch, tmp_path
):
    """Custom pre-prompts written to settings.json are returned by GET /api/settings."""
    from app import proxy

    settings_dir = tmp_path / "testuser" / "testuser-quarto-ed-settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / ".git").mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps({
            "chatSystemPrompt": "Custom chat prompt.",
            "inlineSystemPrompt": "Custom inline prompt.",
        })
    )

    monkeypatch.setattr(proxy, "_WORKSPACE_BASE", str(tmp_path))

    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 200
    settings = resp.json()["settings"]
    assert settings["chatSystemPrompt"] == "Custom chat prompt."
    assert settings["inlineSystemPrompt"] == "Custom inline prompt."


async def test_prompt_fields_are_persisted_via_post_settings(
    auth_client, monkeypatch, tmp_path
):
    """POST /api/settings stores custom pre-prompts in settings.json."""
    from app import proxy

    settings_dir = tmp_path / "testuser" / "testuser-quarto-ed-settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / ".git").mkdir()

    monkeypatch.setattr(proxy, "_WORKSPACE_BASE", str(tmp_path))

    async def _noop_push(_path, _msg=""):
        pass

    monkeypatch.setattr(proxy, "_push_settings", _noop_push)

    custom_settings = {
        "theme": "dark",
        "chatSystemPrompt": "Always respond in French.",
        "inlineSystemPrompt": "Replace with shorter text.",
    }
    resp = await auth_client.post("/api/settings", json={"settings": custom_settings})
    assert resp.status_code == 200

    saved = json.loads((settings_dir / "settings.json").read_text())
    assert saved["chatSystemPrompt"] == "Always respond in French."
    assert saved["inlineSystemPrompt"] == "Replace with shorter text."


# ---------------------------------------------------------------------------
# ai_enabled flag on /editor
# ---------------------------------------------------------------------------

async def test_editor_ai_enabled_with_server_key(auth_client, monkeypatch):
    """ai_enabled is True in template context when server ANTHROPIC_API_KEY is set."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "sk-server")

    resp = await auth_client.get("/editor")
    assert resp.status_code == 200
    # The AI tab button should be rendered (no {% if ai_enabled %} guard now, but
    # the tab is always visible — check it appears in the HTML)
    assert b'onclick="switchPanel(\'ai\')"' in resp.content


async def test_editor_ai_enabled_with_user_key(
    auth_client, test_user, db_session, monkeypatch
):
    """ai_enabled is True when user has a stored key, even if no server key."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "")

    cfg = UserAIConfig(
        user_id=test_user.id,
        provider="claude",
        api_key_encrypted=encrypt_token("sk-ant-user"),
    )
    db_session.add(cfg)
    await db_session.commit()

    resp = await auth_client.get("/editor")
    assert resp.status_code == 200
    assert b'onclick="switchPanel(\'ai\')"' in resp.content


async def test_editor_ai_tab_always_present(auth_client, monkeypatch):
    """The AI tab is always present in the HTML regardless of ai_enabled value."""
    import app.proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_ANTHROPIC_KEY", "")

    resp = await auth_client.get("/editor")
    assert resp.status_code == 200
    # The tab must always be rendered now (no conditional guard)
    assert b'onclick="switchPanel(\'ai\')"' in resp.content


# ---------------------------------------------------------------------------
# UserAIConfig model — unique constraint
# ---------------------------------------------------------------------------

async def test_user_ai_config_unique_per_user(test_user, db_session):
    """A second UserAIConfig row for the same user_id must raise an IntegrityError."""
    import sqlalchemy.exc

    cfg1 = UserAIConfig(user_id=test_user.id, provider="claude")
    db_session.add(cfg1)
    await db_session.commit()

    cfg2 = UserAIConfig(user_id=test_user.id, provider="ollama")
    db_session.add(cfg2)
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()
