from pydantic import BaseModel


class WorkspaceLoadRequest(BaseModel):
    repo_owner: str
    repo_name: str


class SyncRequest(BaseModel):
    message: str = "Sync from quarto-ed"


class InternalWorkspaceLoadRequest(BaseModel):
    user_id: int
    session_id: int
    access_token: str
    repo_owner: str
    repo_name: str


class InternalWorkspaceSyncRequest(BaseModel):
    session_id: int
    message: str = "Sync from quarto-ed"


class InternalReloadRequest(BaseModel):
    user_id: int
