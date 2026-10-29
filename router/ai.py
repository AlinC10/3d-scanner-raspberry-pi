from fastapi import APIRouter, HTTPException, status

from ai.service import AiService, AiUnavailableError
from schemas.ai import AiStatusResponse, ChatRequest, ChatResponse

router = APIRouter(prefix="/api/ai", tags=["AI"])

# Off until main_prod's lifespan calls service.start(): a missing GROQ_API_KEY or library leaves it off with a reason
# instead of stopping the server.
service = AiService()


@router.get("/status", response_model=AiStatusResponse)
def ai_status():
    """
    Whether the AI assistant can answer (`on`), is still loading (`loading`), and why it is off (`reason`).
    """
    return AiStatusResponse(**service.status())


@router.post("/chat", response_model=ChatResponse, responses={503: {"description": "The AI assistant is off"}})
def chat_endpoint(request: ChatRequest):
    """
    Answers a question from the documentation in `ai/ai-docs/`.

    503 when the assistant is off or still loading (the `detail` says why, see `GET /api/ai/status`).
    """
    try:
        result = service.ask(request.question, request.history)
    except AiUnavailableError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"AI assistant error: {e}")
    return ChatResponse(response=result.get("response", "Nu am putut genera un răspuns."))
