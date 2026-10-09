import os
from groq import Groq
from database import db 
from dotenv import load_dotenv

load_dotenv()

groq_key = os.getenv("GROQ_API_KEY")
if not groq_key:
    raise ValueError("Cheia GROQ_API_KEY nu a fost găsită în fișierul .env!")

groq_client = Groq(api_key=groq_key)

def read_agent_instructions():
    file_path = "AGENT.md"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "You are a helpful and friendly AI assistant for a 3D Scanner. Answer naturally."

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
        # Căutăm în baza de date
        results = db.similarity_search(search_query, k=8)
        for chunk in results:
            extracted_context += f"{chunk.page_content}\n\n"
            
        if not extracted_context.strip():
            extracted_context = "Nu am găsit informații în documentație. Răspunde din cunoștințele generale într-un mod prietenos."
            
    except Exception as e:
        # Prindem cazul în care ChromaDB este goală sau neinițializată
        extracted_context = "Baza de date cu documente este goală momentan. Răspunde din cunoștințele tale generale, scurt și clar."

    system_message = read_agent_instructions()
    groq_messages = [{"role": "system", "content": system_message}]
    
    for msg in history:
        groq_messages.append({"role": msg['role'], "content": msg['text']})
        
    final_prompt = f"""
    Extracted context from documentation:
    ---
    {extracted_context}
    ---
    
    Current question: {user_question}
    Answer:
    """
    groq_messages.append({"role": "user", "content": final_prompt})
    
    response = groq_client.chat.completions.create(
        model="openai/gpt-oss-120b", # Am inlocuit cu modelul suportat de groq by default (poti modifica inapoi daca ai custom)
        messages=groq_messages,
        temperature=0.1
    )
    
    response_text = response.choices[0].message.content
    
    if "[NO_SOURCES]" in response_text:
        response_text = response_text.replace("[NO_SOURCES]", "").strip() 

    return {
        "response": response_text
    }