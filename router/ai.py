from fastapi import APIRouter
from pydantic import BaseModel
from typing import List, Dict
import sys
import os

# Ne asigurăm că putem importa din folderul 'ai' aflat în root-ul proiectului
current_dir = os.path.dirname(os.path.abspath(__file__))
ai_folder_path = os.path.join(current_dir, '..', 'ai')
if ai_folder_path not in sys.path:
    sys.path.append(ai_folder_path)

# Importăm funcția agentului
from ai_agent import ask_agent

router = APIRouter(prefix="/api/ai", tags=["AI"])

class ChatRequest(BaseModel):
    question: str
    history: List[Dict[str, str]] = []

@router.post("/chat")
async def chat_endpoint(request: ChatRequest):
    try:
        # Trimitem interogarea către Groq
        result = ask_agent(request.question, request.history)
        
        return {"response": result.get("response", "Nu am putut genera un răspuns.")}
    except Exception as e:
        return {"response": f"[Eroare Server AI]: {str(e)}"}