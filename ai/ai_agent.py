import os
from groq import Groq
from database import db 
from dotenv import load_dotenv

load_dotenv()

groq_key = os.getenv("GROQ_API_KEY")
if not groq_key:
    raise ValueError("Cheia GROQ_API_KEY nu a fost găsită în fișierul .env!")

groq_client = Groq(api_key=groq_key)

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

def read_agent_instructions():
    file_path = os.path.join(CURRENT_DIR, "AGENT.md")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "You are a helpful AI. Answer strictly based on context."

def ask_agent(user_question, history=[]):
    search_query = user_question
    
    if len(history) > 0:
        last_ai_message = next((msg['text'] for msg in reversed(history) if msg['role'] == 'assistant'), "")
        if last_ai_message:
            context_subject = last_ai_message[:100].strip()
            search_query = f"{context_subject} {user_question}"
        else:
            last_user_message = history[-1]['text']
            search_query = f"{last_user_message} {user_question}"

    extracted_context = ""
    try:
        # Căutăm k=15 fragmente pentru a fi absolut siguri că prindem informația corectă
        results = db.similarity_search(search_query, k=15)
        for chunk in results:
            extracted_context += f"{chunk.page_content}\n\n"
            
        if not extracted_context.strip():
            extracted_context = "[NO CONTEXT FOUND. USER MUST BE TOLD YOU DON'T KNOW THE ANSWER]"
            
    except Exception as e:
        extracted_context = "[DATABASE ERROR. NO CONTEXT AVAILABLE]"

    system_base_rules = read_agent_instructions()
    full_system_message = f"""{system_base_rules}

Extracted context from documentation:
---
{extracted_context}
---"""

    groq_messages = [{"role": "system", "content": full_system_message}]
    
    for msg in history:
        groq_messages.append({"role": msg['role'], "content": msg['text']})
        
    groq_messages.append({"role": "user", "content": user_question})
    
    try:
        response = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b", 
            messages=groq_messages,
            temperature=0.0 # Blochează halucinațiile
        )
        response_text = response.choices[0].message.content
    except Exception as e:
        response_text = f"Eroare API Groq: {str(e)}"
    
    if "[NO_SOURCES]" in response_text:
        response_text = response_text.replace("[NO_SOURCES]", "").strip() 
        
    # Curățăm textul de steluțele de Markdown
    response_text = response_text.replace("**", "")

    return {
        "response": response_text
    }