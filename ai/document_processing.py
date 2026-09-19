import os
from langchain_community.document_loaders import PyPDFLoader, Docx2txtLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from database import db 

def delete_document_from_db(filename, delete_physically=True):
    print(f"\n--- [DELETE] Starting deletion for: {filename} ---")
    
    if delete_physically:
        file_path = os.path.join("uploads", filename)
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"[OK] Physical file was deleted.")

    db_data = db.get()
    ids_to_delete = []
    
    for i, metadata in enumerate(db_data['metadatas']):
        if metadata and 'source' in metadata:
            if filename.lower() in metadata['source'].lower(): 
                ids_to_delete.append(db_data['ids'][i])
                
    if ids_to_delete:
        db.delete(ids=ids_to_delete)
        print(f"[SUCCESS] Memory cleaned ({len(ids_to_delete)} old chunks deleted)!\n")
        return True
        
    return False


def process_document(file_path):
    filename = os.path.basename(file_path)
    print(f"\n--- [UPLOAD] Starting processing for: {filename} ---")

    delete_document_from_db(filename, delete_physically=False)

    extension = filename.split('.')[-1].lower()
    documents = []

    if extension == 'pdf':
        loader = PyPDFLoader(file_path)
        documents = loader.load()
    elif extension == 'docx':
        loader = Docx2txtLoader(file_path)
        documents = loader.load()
    elif extension == 'txt':
        loader = TextLoader(file_path, encoding='utf-8')
        documents = loader.load()
    else:
        raise Exception(f"Extension .{extension} is not supported.")

    if not documents or all(len(doc.page_content.strip()) == 0 for doc in documents):
        raise Exception("Could not extract text from document (it is completely empty or an unreadable image).")

    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    chunks = text_splitter.split_documents(documents)

    # ---> FILTRUL NOU ADAUGAT <---
    # Eliminam orice fragment gol, cu tip gresit (None) sau care contine doar spatii libere
    valid_chunks = [chunk for chunk in chunks if
                    chunk.page_content and isinstance(chunk.page_content, str) and chunk.page_content.strip()]

    if not valid_chunks:
        raise Exception("Nu am putut genera fragmente de text valide din acest document.")

    # Adaugam in baza de date doar fragmentele validate
    db.add_documents(valid_chunks)
    print(f"[SUCCESS] Saved {len(valid_chunks)} fresh chunks!\n")

    return len(valid_chunks)