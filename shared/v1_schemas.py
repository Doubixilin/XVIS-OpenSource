"""当前 DSH 单引擎 v1 HTTP 模型；历史模型保留在 schemas.py。"""
from __future__ import annotations
import re
import json
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IMAGE_ID = re.compile(r"i-[0-9a-f]{32}\Z")
MEMORY_ID = re.compile(r"m-[0-9a-f]{32}\Z")
PROFILE_MAX = 16384
DEVICE_ID = re.compile(r"d-[0-9a-f]{32}\Z")

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class ImageView(StrictModel):
    image_id: str
    mime_type: str
    size_bytes: int
    sha256: str
    width: int
    height: int

class ResultRead(StrictModel):
    revision: int = Field(gt=0)

class NotificationDevice(StrictModel):
    token: str = Field(min_length=32, max_length=512)
    environment: str
    enabled: bool

    @field_validator("token")
    @classmethod
    def valid_token(cls, value: str) -> str:
        if len(value) % 2 or re.fullmatch(r"[0-9a-f]+", value) is None:
            raise ValueError("invalid_device_token")
        return value

    @field_validator("environment")
    @classmethod
    def valid_environment(cls, value: str) -> str:
        if value not in {"sandbox", "production"}:
            raise ValueError("invalid_notification_environment")
        return value

def checked_text(value: str, max_bytes: int, *, nonempty: bool = False) -> str:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("invalid_unicode") from None
    if size > max_bytes:
        raise ValueError("text_too_large")
    if nonempty and not value.strip():
        raise ValueError("empty_text")
    return value

class AssistantMemory(StrictModel):
    id: str
    text: str = Field(min_length=1)
    enabled: bool

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if MEMORY_ID.fullmatch(value) is None:
            raise ValueError("invalid_memory_id")
        return value

    @field_validator("text")
    @classmethod
    def bounded_text(cls, value: str) -> str:
        return checked_text(value, 2048, nonempty=True)

class AssistantProfile(StrictModel):
    revision: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=40)
    persona_enabled: bool
    persona: str
    memory_enabled: bool
    memories: list[AssistantMemory] = Field(max_length=20)

    @field_validator("name")
    @classmethod
    def bounded_name(cls, value: str) -> str:
        return checked_text(value, 160, nonempty=True)

    @field_validator("persona")
    @classmethod
    def bounded_persona(cls, value: str) -> str:
        return checked_text(value, 4096)

    @model_validator(mode="after")
    def bounded_profile(self) -> "AssistantProfile":
        if len({entry.id for entry in self.memories}) != len(self.memories):
            raise ValueError("duplicate_memory_id")
        encoded = json.dumps(self.model_dump(), ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > PROFILE_MAX:
            raise ValueError("profile_too_large")
        return self

def checked_image_ids(value: list[str]) -> list[str]:
    if any(IMAGE_ID.fullmatch(image_id) is None for image_id in value):
        raise ValueError("invalid_image_id")
    return list(dict.fromkeys(value))

class TaskCreate(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1)
    image_ids: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("image_ids")
    @classmethod
    def bounded_images(cls, value: list[str]) -> list[str]:
        return checked_image_ids(value)

    @field_validator("prompt")
    @classmethod
    def bounded_prompt(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 16384:
            raise ValueError("prompt_too_large")
        if not value.strip():
            raise ValueError("empty_prompt")
        return value

class FollowupCreate(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1)
    image_ids: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("image_ids")
    @classmethod
    def bounded_images(cls, value: list[str]) -> list[str]:
        return checked_image_ids(value)

    @field_validator("message")
    @classmethod
    def bounded_message(cls, value: str) -> str:
        return TaskCreate.bounded_prompt(value)
