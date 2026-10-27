# ScanAssist AI - Complete Technical Integration Guide

## 1. What is ScanAssist?
ScanAssist is a context-aware, integrated AI assistant built directly into the ScanCore-3D OS. Its primary purpose is to flatten the learning curve of 3D scanning. 

Instead of reading lengthy manuals, users can simply open the AI chat and ask natural language questions like:
*   *"My scan came out blurry, what should I do?"*
*   *"What LED intensity should I use for a shiny metal part?"*
*   *"How long will a 36-degree rotation scan take?"*

ScanAssist provides instant, context-specific advice based on the system's current capabilities.

---

## 2. User Interface & Interaction

### 2.1 The Floating Action Button (FAB)
The AI is accessible from anywhere in the OS via a Floating Action Button (FAB) anchored to the bottom-left corner of the screen. 
*   It displays a distinct icon and the label "AI Guidance".
*   It uses a subtle hover/active animation to encourage interaction.
*   Clicking the FAB slides up the main AI Chat Modal and hides the FAB to save screen space.

### 2.2 The Chat Window
The chat interface is designed to feel like a standard modern messaging app:
*   **Message Bubbles:** User prompts appear on the right with a custom accent color, while AI responses are aligned to the left with a distinct background.
*   **Error Handling:** If the local API goes offline, the UI injects a red, user-friendly error bubble explaining the connection issue.
*   **Header Controls:** The header includes a "Clear Chat" button (broom icon) to wipe the history and a "Close" button to dismiss the modal.

---

## 3. Technical Implementation & Data Flow

ScanAssist does not run locally on the browser; it acts as a frontend client that communicates with the scanner's local FastAPI backend, which in turn securely queries an LLM (Large Language Model) API.

### 3.1 Session Persistence
To ensure a continuous conversation while the user navigates between different pages (e.g., switching from "Main Menu" to "Library"):
*   The conversation array is stringified and saved into the browser's `sessionStorage` under the key `scancore_ai_history`.
*   Every time the AI modal is opened, the `ai-chat.js` script reads this storage and instantly rebuilds the visual chat history in the DOM.

### 3.2 Context Management (Token Limits)
Sending an infinitely growing conversation to an LLM API will quickly result in token limit errors and high costs. The frontend handles this automatically:
*   The `aiHistory` array holds all messages.
*   When a new request is sent, the frontend automatically slices the array, sending **only the last 10 messages** (the most recent context) in the API payload.

### 3.3 The API Handshake
When the user types a message and hits send:
1.  **UI Update:** The user's message is immediately rendered in the chat window. An empty bubble with a blinking "Thinking..." animation is appended below it.
2.  **Payload Assembly:** The frontend builds a JSON object containing the current `question` and the `history` array.
3.  **Fetch Request:** A `POST` request is sent to `http://127.0.0.1:8000/api/ai/chat`.
4.  **Response Handling:** Upon receiving a successful response (HTTP 200), the "Thinking..." bubble is removed, and the actual AI text is injected. The new exchange is pushed to `sessionStorage`.

---

## 4. Future Scalability: Function Calling
The architecture of ScanAssist is designed to support future "Function Calling" (or Tool Use). 
Because the AI backend sits on the same local server that controls the hardware, future updates can allow the AI to not just give text advice, but return JSON commands. 

For example, if the user asks *"Setup the scanner for a small, dark object"*, the AI could respond with text and a background command that automatically adjusts the UI sliders (Camera Brightness to 90%, Z-Axis to 50mm) on the user's behalf.