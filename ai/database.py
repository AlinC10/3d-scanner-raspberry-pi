import os
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma

# Căi absolute pentru a nu avea erori indiferent de unde pornești serverul
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(CURRENT_DIR, "chroma_database")
DOCS_PATH = os.path.join(CURRENT_DIR, "ai-docs")

embedding_model = HuggingFaceEmbeddings(model_name="paraphrase-multilingual-MiniLM-L12-v2")
db = Chroma(persist_directory=DB_PATH, embedding_function=embedding_model)

try:
    # Auto-populare: dacă baza de date are 0 fragmente, o generăm automat
    if db._collection.count() == 0:
        print("\n[INFO] Baza de date AI este goală. Populez automat din 'ai-docs'...")
        from document_processing import process_directory
        
        # Incarcăm tot folderul automat dintr-un foc
        chunks_count = process_directory(DOCS_PATH, "full_docs")
        
        print(f"[SUCCESS] Baza de date a fost populată cu succes! ({chunks_count} fragmente)\n")
except Exception as e:
    print(f"\n[WARNING] Eroare la popularea bazei de date: {e}\n")