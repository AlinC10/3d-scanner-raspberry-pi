async function sendMessage() {
    const inputField = document.getElementById('user-input');
    const message = inputField.value.trim();
    
    if (message === '') return;

    let sessions = JSON.parse(localStorage.getItem(SESSIONS_STORAGE_KEY)) || [];
    let currentSession = sessions.find(s => s.id === currentSessionId);
    let history = [];
    if (currentSession && currentSession.messages) {
        history = currentSession.messages.slice(-4).map(msg => ({
            role: msg.role === 'ai' ? 'assistant' : 'user',
            text: msg.text
        }));
    }

    addMessageToChat('user', message);
    inputField.value = '';

    const typingIndicator = document.getElementById('typing-indicator');
    typingIndicator.style.display = 'flex';

    try {
        const response = await fetch('http://localhost:5000/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: message, history: history }) 
        });

        const data = await response.json();
        typingIndicator.style.display = 'none';

        if(response.ok) {
            const sourcesText = data.sources && data.sources.length > 0 ? data.sources.join(" | ") : null;
            addAIMessage(data.response, sourcesText);
        } else {
            addAIMessage(`AI Error: ${data.error}`);
        }
    } catch (error) {
        typingIndicator.style.display = 'none';
        addAIMessage(`Python server is not running. Check the terminal console.`);
    }
}

function handleKeyPress(event) {
    if (event.key === 'Enter') {
        sendMessage();
    }
}

function addMessageToChat(role, text) {
    addMessageToChatDOM(text);
    saveMessageToSession('user', text, null);
}

function addMessageToChatDOM(text) {
    const chatWindow = document.getElementById('chat-window');
    const welcomeScreen = chatWindow.querySelector('.welcome-screen');
    if(welcomeScreen) {
        welcomeScreen.remove(); 
    }
    const msgDiv = document.createElement('div');
    msgDiv.className = `message user`;
    msgDiv.innerHTML = `<div class="avatar user">YOU</div><div class="message-content">${text}</div>`;
    chatWindow.appendChild(msgDiv);
    chatWindow.scrollTop = chatWindow.scrollHeight;
}

function addAIMessage(text, source = null) {
    addAIMessageDOM(text, source);
    saveMessageToSession('ai', text, source);
}

function addAIMessageDOM(text, source = null) {
    const chatWindow = document.getElementById('chat-window');
    const welcomeScreen = chatWindow.querySelector('.welcome-screen');
    if(welcomeScreen) {
        welcomeScreen.remove();
    }

    const msgDiv = document.createElement('div');
    msgDiv.className = `message ai`;
    
    let htmlContent = `
        <div class="avatar ai img-avatar">
            <img src="./media/images/Logo.png" alt="Doxy">
        </div>
        <div class="message-content msg-content-copy">
            <button class="copy-btn" title="Copy text" onclick="copyMessageText(this)">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
            </button>
            <div class="text-body">${marked.parse(text)}</div>`;
        
    if(source) {
        htmlContent += `<br><div class="source-box"><svg width="12" height="12" class="source-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>Source: ${source}</div>`;
    }
    htmlContent += `</div>`;
    msgDiv.innerHTML = htmlContent;
    
    const copyBtn = msgDiv.querySelector('.copy-btn');
    copyBtn.setAttribute('data-raw-text', text);

    chatWindow.appendChild(msgDiv);
    chatWindow.scrollTop = chatWindow.scrollHeight;
}