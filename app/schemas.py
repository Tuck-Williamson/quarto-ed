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


class GitignoreAddRequest(BaseModel):
    path: str
    is_dir: bool = False


class DeleteFileRequest(BaseModel):
    path: str
    is_dir: bool = False


class SettingsSaveRequest(BaseModel):
    settings: dict
    snippets: list | None = None


class RepoSettingsSaveRequest(BaseModel):
    settings: dict


class CommitRequest(BaseModel):
    message: str = "User saved."


class AIKeySaveRequest(BaseModel):
    provider: str               # "claude" | "ollama"
    api_key: str | None = None
    model: str | None = None
    ollama_endpoint: str | None = None


class AIInlineRequest(BaseModel):
    prompt: str
    selection: str | None = None
    system_prompt: str | None = None


class AIChatRequest(BaseModel):
    message: str
    context: str | None = None
    system_prompt: str | None = None


class PreviewRestartRequest(BaseModel):
    target: str | None = None
