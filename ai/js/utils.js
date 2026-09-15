function showNotification(text) {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.innerText = text;
  container.appendChild(toast);
  setTimeout(() => {
    if (toast.parentElement) toast.remove();
  }, 3300);
}

function copyMessageText(button) {
  const textToCopy = button.getAttribute("data-raw-text");
  navigator.clipboard
    .writeText(textToCopy)
    .then(() => {
      showNotification("Text copied!");
      const originalHTML = button.innerHTML;
      button.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ><polyline points="20 6 9 17 4 12" /></svg>`;
      setTimeout(() => {
        button.innerHTML = originalHTML;
      }, 1500);
    })
    .catch((err) => {
      showNotification("Failed to copy text.");
    });
}
