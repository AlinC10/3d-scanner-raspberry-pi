import os
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma

from docs_index import docs_fingerprint, needs_rebuild, read_fingerprint, write_fingerprint

# Căi absolute pentru a nu avea erori indiferent de unde pornești serverul
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(CURRENT_DIR, "chroma_database")
DOCS_PATH = os.path.join(CURRENT_DIR, "ai-docs")

# The model is downloaded once into ~/.cache/huggingface; every start only loads it into memory
embedding_model = HuggingFaceEmbeddings(model_name="paraphrase-multilingual-MiniLM-L12-v2")
db = Chroma(persist_directory=DB_PATH, embedding_function=embedding_model)

try:
    # The database persists between runs: it is (re)built only when it is empty or 'ai-docs' changed since it was built
    current = docs_fingerprint(DOCS_PATH)
    if needs_rebuild(db._collection.count(), read_fingerprint(DB_PATH), current):
        print("\n[INFO] Baza de date AI este goala sau documentatia s-a schimbat. Populez din 'ai-docs'...")
        old_ids = db.get().get("ids") or []
        if old_ids:
            db.delete(ids=old_ids)  # chunks of a deleted or renamed document would otherwise stay
        from document_processing import process_directory

        # Incarcăm tot folderul automat dintr-un foc
        chunks_count = process_directory(DOCS_PATH, "full_docs")
        if chunks_count:
            write_fingerprint(DB_PATH, current)

        print(f"[SUCCESS] Baza de date a fost populată cu succes! ({chunks_count} fragmente)\n")
except Exception as e:
    print(f"\n[WARNING] Eroare la popularea bazei de date: {e}\n")
