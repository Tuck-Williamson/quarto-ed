from pydantic import BaseModel


class WorkspaceLoadRequest(BaseModel):
    repo_owner: str
    repo_name: str


class SyncRequest(BaseModel):
    message: str = "Sync from quarto-ed"


class FileWriteRequest(BaseModel):
    path: str
    content: str


class FileCreateRequest(BaseModel):
    path: str


class SettingsSaveRequest(BaseModel):
    settings: dict
    snippets: list | None = None


class CommitRequest(BaseModel):
    message: str = "User saved."


class AIChatRequest(BaseModel):
    message: str
    context: str | None = None
