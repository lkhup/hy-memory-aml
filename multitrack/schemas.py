import base64
import binascii
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IMAGE_LIMIT = 10 * 1024 * 1024
MEDIA_LIMIT = 30 * 1024 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class TextPart(StrictModel):
    type: Literal["text"]
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class ImageURL(StrictModel):
    url: str
    detail: Literal["auto", "low", "high"] | None = None

    @field_validator("url")
    @classmethod
    def inline_image(cls, value):
        header, sep, encoded = value.partition(",")
        formats = {"data:image/jpeg;base64", "data:image/png;base64", "data:image/webp;base64"}
        if not sep or header not in formats:
            raise ValueError("image must be an inline JPEG, PNG or WebP Base64 Data URI")
        if len(encoded) > (IMAGE_LIMIT + 2) // 3 * 4:
            raise ValueError("image exceeds 10 MiB")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid Base64 image") from exc
        if not raw or len(raw) > IMAGE_LIMIT:
            raise ValueError("image must contain 1 byte to 10 MiB")
        valid = (
            (header.startswith("data:image/png") and raw.startswith(b"\x89PNG\r\n\x1a\n"))
            or (header.startswith("data:image/jpeg") and raw.startswith(b"\xff\xd8\xff"))
            or (
                header.startswith("data:image/webp") and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
            )
        )
        if not valid:
            raise ValueError("image signature does not match its media type")
        return value


class ImagePart(StrictModel):
    type: Literal["image_url"]
    image_url: ImageURL


Part = Annotated[TextPart | ImagePart, Field(discriminator="type")]
Content = str | list[Part]


def media_size(content: Content) -> int:
    if isinstance(content, str):
        return 0
    total = 0
    for part in content:
        url = (
            part.image_url.url
            if isinstance(part, ImagePart)
            else (
                part["image_url"]["url"]
                if isinstance(part, dict) and part.get("type") == "image_url"
                else None
            )
        )
        if url:
            encoded = url.partition(",")[2]
            total += len(encoded) // 4 * 3 - (len(encoded) - len(encoded.rstrip("=")))
    return total


def validate_content(value):
    if isinstance(value, str) and not value.strip() or isinstance(value, list) and not value:
        raise ValueError("content must not be empty")
    return value


class Message(StrictModel):
    role: Literal["user", "assistant"]
    content: Content
    timestamp: int | None = None
    _nonempty = field_validator("content")(validate_content)


class AddRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=512)
    user_id: str = Field(min_length=1, max_length=512)
    session_id: str = Field(min_length=1, max_length=512)
    messages: list[Message] = Field(min_length=1)

    @model_validator(mode="after")
    def media_budget(self):
        if sum(media_size(m.content) for m in self.messages) > MEDIA_LIMIT:
            raise ValueError("Add images exceed 30 MiB in total")
        return self


class SearchRequest(StrictModel):
    query: Content
    options: list[str] | None = None
    user_id: str = Field(min_length=1, max_length=512)
    top_k: int = Field(ge=1, le=100)
    _nonempty = field_validator("query")(validate_content)

    @model_validator(mode="after")
    def media_budget(self):
        if media_size(self.query) > MEDIA_LIMIT:
            raise ValueError("Search images exceed 30 MiB")
        return self


class MemoryRecord(StrictModel):
    id: str
    content: Content
    score: float | None = None
    created_at: str | None = None
