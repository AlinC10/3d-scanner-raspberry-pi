---
apply: by file patterns
instructions: # SCOPE: ChromaDB, ChatGroq, Embeddings, Vector Database, RAG, MarkItDown. Apply these rules whenever the prompt involves the AI agent, document processing, CLI knowledge base, or vector queries.
patterns: ai/**
---

# RAG System Architecture & Agent Documentation

## 📌 Overview
This module implements a simplistic, standalone Retrieval-Augmented Generation (RAG) system. It is designed to be completely project-agnostic. The system allows users to ingest multiple documents into a vector database via a Command Line Interface (CLI), and provides an API endpoint for querying the knowledge base using low-latency generation.

## 🛠️ Tech Stack
*   **Language:** Python 3.13
*   **Orchestration:** LangChain
*   **LLM (Generation):** ChatGroq (for ultra-fast inference)
*   **Vector Database:** ChromaDB (for efficient chunk and vector storage)
*   **Document Parsing:** MarkItDown (for converting PDFs and other formats natively into Markdown)

---

## 🖥️ CLI Interface (Knowledge Base Management)
The system includes a Command Line Interface (CLI) for administrators to manage the knowledge base. The menu includes:
1.  **Add Document:** Prompts the user for a file path, processes the document, and appends its chunks to the existing ChromaDB database. It supports adding multiple documents over time.
2.  **Reset Database:** Completely clears the existing ChromaDB collection, starting fresh.
3.  **Exit:** Closes the CLI application safely.

---

## ⚙️ Flow 1: Document Ingestion (via CLI)
This flow is triggered when adding a new document through the CLI.

1.  **Text Extraction:** The source document (e.g., PDF) is read using **MarkItDown**, which extracts and cleans the text, returning it in Markdown format.
2.  **Segmentation (Chunking):** The extracted text goes through a splitting pipeline to maintain semantic coherence:
    *   `MarkdownTextSplitter` (optional): To split text based on Markdown header structure.
    *   `RecursiveCharacterTextSplitter`: To chunk the text into optimal sizes for the LLM.
        *   **Chunk Size:** `500` characters
        *   **Chunk Overlap:** `50` characters
3.  **Vectorization & Storage:** The resulting chunks are transformed into embeddings (using a standard embedding model) and added/appended to the persistent **ChromaDB** collection.

---

## 🔄 Flow 2: Retrieval and Generation (API Flow)
This is the active flow, called whenever a user or the main project asks a question.

1.  **API Request:** The system receives a user query via an API request.
2.  **Retrieval (Semantic Search):** ChromaDB is queried to retrieve the **top-K** most relevant chunks based on vector similarity to the user's question.
3.  **Prompt Construction:** The retrieved chunks are injected into a standard system prompt context: 
    *   *"You are a helpful assistant. Using the following context extracted from the document(s), answer the question..."*
4.  **Generation:** The complete prompt is sent to the Groq API via the `ChatGroq` integration.
5.  **API Response:** The generated text response is returned to the main system.

---

## 📝 Recommended Configurations
*   **Chunk Size / Overlap:** 500 / 50 characters.
*   **Retrieval Top-K:** `k=3` or `k=4` (optimal for 500-char chunks to fit perfectly in the LLM context window without overwhelming it).
*   **Embeddings Model:** A LangChain-compatible model (e.g., HuggingFace `all-MiniLM-L6-v2` for local, cost-free embeddings, or an equivalent API-based model).
*   **LLM Model (Groq):** Not determined yet.