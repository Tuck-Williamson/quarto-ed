"""GitHub repository listing."""
import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..database import decrypt_token, get_db_session
from ..models import Session, User
from ._core import require_session

router = APIRouter()


@router.get("/api/repos")
async def list_repos(sess: Session = Depends(require_session)):
    async with get_db_session() as db:
        result = await db.execute(select(User).where(User.id == sess.user_id))
        user = result.scalar_one_or_none()
    access_token = decrypt_token(user.access_token_encrypted)
    repos = []
    page = 1
    async with httpx.AsyncClient() as client:
        while True:
            resp = await client.get(
                "https://api.github.com/user/repos",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
                params={"per_page": 100, "page": page, "sort": "updated"},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            repos.extend(r["name"] for r in batch)
            if len(batch) < 100:
                break
            page += 1
    return {"repos": repos}
