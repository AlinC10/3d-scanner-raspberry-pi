from pydantic import BaseModel, Field
from typing import Annotated


class ChatRequest(BaseModel):
    """Body of `POST /api/ai/chat`."""
    question: Annotated[str, Field(description="The user's question.")]
    history: Annotated[list[dict[str, str]], Field(
        description='Earlier messages, oldest first: [{"role": "user" | "assistant", "text": "..."}].'
    )] = []


class ChatResponse(BaseModel):
    response: str


class AiStatusResponse(BaseModel):
    """`GET /api/ai/status`: whether the assistant can answer."""
    on: bool = False        # True only once the key, the model and the database loaded
    loading: bool = False   # True while the service is still loading at startup
    reason: str | None = None   # why it is off (None when on)
