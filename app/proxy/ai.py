"""Anthropic-proxied AI chat (SSE) and single-shot inline editing.

Ollama is browser-direct; these endpoints only proxy Claude."""
import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ..models import Session
from ..schemas import AIChatRequest, AIInlineRequest
from ._core import _AI_MODEL, _SETTINGS_DEFAULTS, _get_user_api_key, require_session

router = APIRouter()


@router.post("/api/ai/chat")
async def ai_chat(body: AIChatRequest, sess: Session = Depends(require_session)):
    # Provider, model, and endpoint are now settings — sent by the client.
    # Ollama calls are browser-direct; this endpoint only proxies Claude.
    if (body.provider or "claude") == "ollama":
        raise HTTPException(status_code=400, detail="ollama_browser_direct")

    api_key = await _get_user_api_key(sess.user_id)
    if not api_key:
        raise HTTPException(status_code=503, detail="AI not configured")

    model = body.model or _AI_MODEL
    user_content = body.message
    if body.context:
        user_content = f"<document>\n{body.context}\n</document>\n\n{body.message}"

    system_prompt = body.system_prompt or _SETTINGS_DEFAULTS["chatSystemPrompt"]

    async def generate():
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 4096,
                    "stream": True,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_content}],
                },
            ) as response:
                if response.status_code != 200:
                    yield f"data: {{\"type\":\"error\",\"error\":{{\"message\":\"AI request failed ({response.status_code})\"}}}}\n\n"
                    return
                async for chunk in response.aiter_text():
                    yield chunk

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/api/ai/inline")
async def ai_inline(body: AIInlineRequest, sess: Session = Depends(require_session)):
    """Single-shot inline prompt endpoint (Claude only — Ollama is browser-direct)."""
    if (body.provider or "claude") == "ollama":
        raise HTTPException(status_code=400, detail="ollama_browser_direct")

    api_key = await _get_user_api_key(sess.user_id)
    if not api_key:
        raise HTTPException(status_code=503, detail="AI not configured")

    model = body.model or _AI_MODEL
    user_content = body.prompt
    if body.selection:
        user_content = f"{body.prompt}\n\n<selection>\n{body.selection}\n</selection>"

    system_prompt = body.system_prompt or _SETTINGS_DEFAULTS["inlineSystemPrompt"]

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 4096,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_content}],
            },
        )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="AI request failed")
    data = response.json()
    text = (data.get("content") or [{}])[0].get("text", "")
    return {"text": text}
