function closeCustomModal(returnValue) {
    document.getElementById('custom-modal-overlay').style.display = 'none';
    document.getElementById('confirm-modal').classList.add('d-none');
    document.getElementById('prompt-modal').classList.add('d-none');
    
    if (currentModalResolve) {
        currentModalResolve(returnValue);
        currentModalResolve = null;
    }
}

function showConfirmModal(message) {
    return new Promise((resolve) => {
        currentModalResolve = resolve;
        document.getElementById('confirm-message').textContent = message;
        
        document.getElementById('custom-modal-overlay').style.display = 'flex';
        document.getElementById('confirm-modal').classList.remove('d-none');
    });
}

function showPromptModal(message, defaultValue = "") {
    return new Promise((resolve) => {
        currentModalResolve = resolve;
        document.getElementById('prompt-message').textContent = message;
        
        const inputEl = document.getElementById('prompt-input');
        inputEl.value = defaultValue;
        
        document.getElementById('custom-modal-overlay').style.display = 'flex';
        document.getElementById('prompt-modal').classList.remove('d-none');
        inputEl.focus(); 
        
        inputEl.onkeypress = (e) => {
            if (e.key === 'Enter') {
                closeCustomModal(inputEl.value);
            }
        };
    });
}