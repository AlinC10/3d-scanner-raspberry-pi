document.addEventListener('DOMContentLoaded', () => {
    loadDocumentsOnStart();
    let sessions = JSON.parse(localStorage.getItem(SESSIONS_STORAGE_KEY)) || [];
    if (sessions.length === 0) {
        createNewChat();
    } else {
        currentSessionId = sessions[0].id;
        renderSessionsList();
        renderCurrentChat();
    }
});

function createNewChat() {
    let sessions = JSON.parse(localStorage.getItem(SESSIONS_STORAGE_KEY)) || [];
    
    const emptySession = sessions.find(s => s.messages.length === 0);
    
    if (emptySession) {
        currentSessionId = emptySession.id;
        renderSessionsList();
        renderCurrentChat();
        return; 
    }

    const newSession = {
        id: Date.now().toString(),
        title: "New Conversation",
        messages: [],
        isPinned: false
    };
    
    sessions.unshift(newSession); 
    localStorage.setItem(SESSIONS_STORAGE_KEY, JSON.stringify(sessions));
    
    currentSessionId = newSession.id;
    renderSessionsList();
    renderCurrentChat();
}

function switchSession(id) {
    currentSessionId = id;
    renderSessionsList();
    renderCurrentChat();
}

function renderSessionsList() {
    const container = document.getElementById('sessions-container');
    if (!container) return;
    
    let sessions = JSON.parse(localStorage.getItem(SESSIONS_STORAGE_KEY)) || [];
    container.innerHTML = '';
    
    sessions.sort((a, b) => {
        if (a.isPinned === b.isPinned) return 0;
        return a.isPinned ? -1 : 1;
    });

    sessions.forEach(session => {
        const div = document.createElement('div');
        div.className = `session-item ${session.id === currentSessionId ? 'active' : ''}`;
        
        div.onclick = (e) => {
            if(!e.target.closest('.session-btn')) switchSession(session.id);
        };

        const titleSpan = document.createElement('span');
        titleSpan.className = 'session-title';
        titleSpan.textContent = session.title;

        const actionsDiv = document.createElement('div');
        actionsDiv.className = 'session-actions';

        const pinBtn = document.createElement('button');
        pinBtn.className = `session-btn pin ${session.isPinned ? 'pinned' : ''}`;
        pinBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="${session.isPinned ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15 8 22 9 17 14 18.5 21 12 17.5 5.5 21 7 14 2 9 9 8 12 2"></polygon></svg>`;
        pinBtn.title = "Pin on top";
        pinBtn.onclick = (e) => {
            e.stopPropagation();
            togglePinSession(session.id);
        };

        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'session-btn delete';
        deleteBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>`;
        deleteBtn.title = "Delete conversation";
        deleteBtn.onclick = (e) => {
            e.stopPropagation();
            deleteSession(session.id);
        };

        actionsDiv.appendChild(pinBtn);
        actionsDiv.appendChild(deleteBtn);
        
        div.appendChild(titleSpan);
        div.appendChild(actionsDiv);
        container.appendChild(div);
    });
}

function renderCurrentChat() {
    const chatWindow = document.getElementById('chat-window');
    chatWindow.innerHTML = ''; 
    
    let sessions = JSON.parse(localStorage.getItem(SESSIONS_STORAGE_KEY)) || [];
    const currentSession = sessions.find(s => s.id === currentSessionId);
    
    if (!currentSession || currentSession.messages.length === 0) {
        chatWindow.innerHTML = `
            <div class="welcome-screen">
                <img src="./media/images/Doxy-fullbody.png" alt="Doxy" class="welcome-logo">
                <div class="welcome-title">Hi! I am Doxy.</div>
                <div>What information are you looking for today?</div>
            </div>
        `;
    } else {
        currentSession.messages.forEach(msg => {
            if (msg.role === 'user') {
                addMessageToChatDOM(msg.text);
            } else {
                addAIMessageDOM(msg.text, msg.source);
            }
        });
    }
}

function saveMessageToSession(role, text, source) {
    let sessions = JSON.parse(localStorage.getItem(SESSIONS_STORAGE_KEY)) || [];
    let currentSession = sessions.find(s => s.id === currentSessionId);
    
    if (currentSession) {
        if (currentSession.messages.length === 0 && role === 'user') {
            currentSession.title = text.substring(0, 22) + (text.length > 22 ? '...' : '');
        }
        
        currentSession.messages.push({ role, text, source });
        localStorage.setItem(SESSIONS_STORAGE_KEY, JSON.stringify(sessions));
        renderSessionsList(); 
    }
}

function togglePinSession(id) {
    let sessions = JSON.parse(localStorage.getItem(SESSIONS_STORAGE_KEY)) || [];
    const index = sessions.findIndex(s => s.id === id);
    if (index > -1) {
        sessions[index].isPinned = !sessions[index].isPinned;
        localStorage.setItem(SESSIONS_STORAGE_KEY, JSON.stringify(sessions));
        renderSessionsList();
    }
}

async function deleteSession(id) {
    const isConfirmed = await showConfirmModal("Are you sure you want to permanently delete this conversation?");
    if(!isConfirmed) return;
    
    let sessions = JSON.parse(localStorage.getItem(SESSIONS_STORAGE_KEY)) || [];
    sessions = sessions.filter(s => s.id !== id);
    localStorage.setItem(SESSIONS_STORAGE_KEY, JSON.stringify(sessions));
    
    if(sessions.length === 0) {
        createNewChat();
    } else {
        if(currentSessionId === id) {
            currentSessionId = sessions[0].id;
        }
        renderSessionsList();
        renderCurrentChat();
    }
}

function exportCurrentChat() {
    let sessions = JSON.parse(localStorage.getItem(SESSIONS_STORAGE_KEY)) || [];
    let currentSession = sessions.find(s => s.id === currentSessionId);
    
    if (!currentSession || !currentSession.messages || currentSession.messages.length === 0) {
        showNotification("This conversation is empty.");
        return;
    }
    
    let textContent = `=== DOXY AI CONVERSATION ===\n`;
    textContent += `Title: ${currentSession.title}\n`;
    textContent += `Export Date: ${new Date().toLocaleString('en-US')}\n`;
    textContent += `========================================\n\n`;
    
    currentSession.messages.forEach(msg => {
        const role = msg.role === 'user' ? 'USER' : 'DOXY AI';
        textContent += `[${role}]:\n${msg.text}\n`;
        if (msg.source) {
            textContent += `Source: ${msg.source}\n`;
        }
        textContent += `\n----------------------------------------\n\n`;
    });
    
    const blob = new Blob([textContent], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    
    const cleanFilename = currentSession.title.replace(/[^a-zA-Z0-9 ]/g, "").trim().replace(/\s+/g, "_");
    a.download = `chat_${cleanFilename || 'doxy'}.txt`;
    
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    
    showNotification("Conversation exported successfully!");
}

async function renameSession(id) {
    let sessions = JSON.parse(localStorage.getItem(SESSIONS_STORAGE_KEY)) || [];
    const session = sessions.find(s => s.id === id);
    if (!session) return;

    const newName = await showPromptModal("Enter a new name for the conversation:", session.title);
    
    if (newName !== null && newName.trim() !== "") {
        session.title = newName.trim();
        localStorage.setItem(SESSIONS_STORAGE_KEY, JSON.stringify(sessions));
        renderSessionsList();
        showNotification("Conversation renamed.");
    }
}