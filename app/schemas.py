from pydantic import BaseModel


class WorkspaceLoadRequest(BaseModel):
    repo_owner: str
    repo_name: str


class SyncRequest(BaseModel):
    message: str = "Sync from quarto-ed"
