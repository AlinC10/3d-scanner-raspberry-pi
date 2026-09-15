const savedTheme = localStorage.getItem('doxy_theme');
if(savedTheme === 'light'){
    document.body.setAttribute('data-theme', 'light');
}

function toggleTheme(){
    const body = document.body;
    if (body.getAttribute('data-theme') === 'light') {
        body.removeAttribute('data-theme');
        localStorage.setItem('doxy_theme', 'dark');
    } else {
        body.setAttribute('data-theme', 'light');
        localStorage.setItem('doxy_theme', 'light');
    }
}