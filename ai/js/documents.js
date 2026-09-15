async function processFileUpload(file) {
    if (!file) return;

    const emptyMsg = document.getElementById('empty-doc-msg');
    if (emptyMsg) emptyMsg.style.display = 'none';
    
    const docContainer = document.getElementById('doc-container');
    const newDoc = document.createElement('div');
    newDoc.className = 'doc-item doc-loading-state';
    newDoc.id = 'loading-' + file.name;
    
    newDoc.innerHTML = `
        <div class="doc-prog-wrapper">
            <div class="doc-prog-header">
                <div class="doc-prog-file">
                    <svg width="14" height="14" class="doc-prog-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>
                    <span class="doc-prog-filename">${file.name}</span>
                </div>
                <span class="progress-percent">0%</span>
            </div>
            <div class="progress-bar-container">
                <div class="progress-bar-fill" id="fill-${file.name}"></div>
            </div>
        </div>
    `;
    docContainer.appendChild(newDoc);

    let progress = 0;
    const fillElem = document.getElementById(`fill-${file.name}`);
    const percentElem = newDoc.querySelector('.progress-percent');
    
    const progressInterval = setInterval(() => {
        if (progress < 90) {
            let increment = progress < 50 ? (Math.random() * 15 + 5) : (Math.random() * 5 + 1);
            progress += increment;
            if (progress > 90) progress = 90;
            fillElem.style.width = `${progress}%`;
            percentElem.innerText = `${Math.floor(progress)}%`;
        }
    }, 400);

    const formData = new FormData();
    formData.append('file', file);

    try {
        const response = await fetch('http://localhost:5000/upload', {
            method: 'POST',
            body: formData
        });
        
        const data = await response.json();
        
        clearInterval(progressInterval);
        fillElem.style.width = '100%';
        percentElem.innerText = '100%';
        
        setTimeout(() => {
            if(response.ok) {
                newDoc.className = 'doc-item'; 
                newDoc.innerHTML = `
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink: 0;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>
                    <span class="doc-name" title="${file.name}">${file.name}</span>
                    <button class="delete-doc-btn" onclick="deleteDocument('${file.name}', 'loading-${file.name}')" title="Delete document">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"></path><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                    </button>
                `;
                showNotification(`Document processed and added!`);
            } else {
                newDoc.remove();
                showNotification(`Processing error: ${data.error}`);
            }
        }, 500);

    } catch (error) {
        clearInterval(progressInterval);
        newDoc.remove();
        showNotification(`Server connection issue occurred.`);
    }
}

async function handleFileUpload(event) {
    const file = event.target.files[0];
    await processFileUpload(file);
    event.target.value = ''; 
}

async function deleteDocument(filename, docElementId) {
    const isConfirmed = await showConfirmModal(`Are you sure you want to delete "${filename}" from memory?`);
    if (!isConfirmed) return;

    try {
        const response = await fetch(`http://localhost:5000/delete/${filename}`, {
            method: 'DELETE'
        });

        if (response.ok) {
            const docElement = document.getElementById(docElementId);
            if (docElement) docElement.remove();
            
            showNotification(`File ${filename} was deleted.`);
            const docContainer = document.getElementById('doc-container');
            const remainingDocs = docContainer.querySelectorAll('.doc-item');
            if (remainingDocs.length === 0) {
                const emptyMsg = document.getElementById('empty-doc-msg');
                if (emptyMsg) emptyMsg.style.display = 'block';
            }
        } else {
            showNotification(`Error deleting document.`);
        }
    } catch (error) {
        console.error(error);
        showNotification("Connection error while deleting document.");
    }
}

async function loadDocumentsOnStart() {
    try {
        const response = await fetch('http://localhost:5000/documents');
        const data = await response.json();
        
        if (response.ok && data.documents && data.documents.length > 0) {
            const docContainer = document.getElementById('doc-container');
            const emptyMsg = document.getElementById('empty-doc-msg');
            
            if (emptyMsg) emptyMsg.style.display = 'none';
            
            data.documents.forEach(filename => {
                const newDoc = document.createElement('div');
                newDoc.className = 'doc-item';
                newDoc.id = 'doc-' + filename;
                newDoc.innerHTML = `
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink: 0;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>
                    <span class="doc-name" title="${filename}">${filename}</span>
                    <button class="delete-doc-btn" onclick="deleteDocument('${filename}', 'doc-${filename}')" title="Delete document">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"></path><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                    </button>
                `;
                docContainer.appendChild(newDoc);
            });
        }
    } catch (error) {
        console.error("Could not load documents.", error);
    }
}