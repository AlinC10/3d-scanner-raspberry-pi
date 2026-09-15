function toggleSidebar() {
    if (window.innerWidth <= 768) {
        document.body.classList.toggle('mobile-sidebar-open');
    } else {
        document.body.classList.toggle('sidebar-collapsed');
    }
}

function toggleTopMenu(event) {
    event.stopPropagation();
    const menu = document.getElementById('top-dropdown-menu');
    if (menu) {
        document.querySelectorAll('.session-menu-dropdown').forEach(m => {
            if (m !== menu) m.classList.remove('show');
        });
        menu.classList.toggle('show');
    }
}

function topMenuPin() {
    if (!currentSessionId) return;
    togglePinSession(currentSessionId);
    document.getElementById('top-dropdown-menu').classList.remove('show');
}

function topMenuRename() {
    if (!currentSessionId) return;
    renameSession(currentSessionId);
    document.getElementById('top-dropdown-menu').classList.remove('show');
}

function topMenuDelete() {
    if (!currentSessionId) return;
    deleteSession(currentSessionId);
    document.getElementById('top-dropdown-menu').classList.remove('show');
}

document.addEventListener('click', () => {
    const topMenu = document.getElementById('top-dropdown-menu');
    if (topMenu) topMenu.classList.remove('show');
});

// Drag and drop for files
document.addEventListener('dragover', (e) => {
    e.preventDefault();
    document.body.classList.add('drag-over');
});

document.addEventListener('dragleave', (e) => {
    e.preventDefault();
    if (e.clientX === 0 && e.clientY === 0) {
        document.body.classList.remove('drag-over');
    }
});

document.addEventListener('drop', async (e) => {
    e.preventDefault();
    document.body.classList.remove('drag-over');

    const files = e.dataTransfer.files;
    if (files.length > 0) {
        const file = files[0];
        const validExtensions = ['pdf', 'docx', 'txt'];
        const fileExtension = file.name.split('.').pop().toLowerCase();
        
        if (!validExtensions.includes(fileExtension)) {
            showNotification(`Extension .${fileExtension} is not supported.`);
            return;
        }

        await processFileUpload(file);
    }
});