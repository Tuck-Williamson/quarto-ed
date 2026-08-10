"""Ollama model discovery and configuration.

Provides endpoints to query available models from the Ollama instance
and manage model-specific parameters."""
import httpx
from fastapi import APIRouter, Depends, HTTPException

from ..models import Session
from ._core import require_session

router = APIRouter()


@router.get("/api/ollama/models")
async def get_ollama_models(endpoint: str = "http://localhost:11434", sess: Session = Depends(require_session)):
    """Query available models from the Ollama instance.
    
    Args:
        endpoint: Ollama endpoint URL (from query param or settings)
    
    Returns:
        List of available model names and their details
    """
    try:
        # Remove trailing slash for consistency
        endpoint = endpoint.rstrip("/")
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{endpoint}/api/tags")
            if response.status_code != 200:
                raise HTTPException(
                    status_code=503,
                    detail=f"Ollama endpoint returned {response.status_code}"
                )
            data = response.json()
            models = data.get("models", [])
            # Extract just the model names for the dropdown
            model_list = [
                {
                    "name": m.get("name"),
                    "details": m.get("details", {}),
                }
                for m in models
            ]
            return {"models": model_list, "count": len(model_list)}
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot connect to Ollama at {endpoint}. Ensure Ollama is running and CORS is enabled."
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail=f"Ollama endpoint at {endpoint} timed out"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error querying Ollama: {str(e)}"
        )
