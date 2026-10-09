// Dossier page behaviour. Kept out of the template so the panel's Content-Security-Policy
// can forbid inline scripts entirely.
(() => {
  const form = document.querySelector('#decision-form');
  const note = document.querySelector('#note');
  if (!form || !note) return;

  note.addEventListener('input', () => note.setCustomValidity(''));
  form.addEventListener('submit', (event) => {
    const action = event.submitter && event.submitter.value;
    if (action === 'reject' && !note.value.trim()) {
      event.preventDefault();
      note.setCustomValidity('Indiquez le motif du refus.');
      note.reportValidity();
      note.focus();
      return;
    }
    // One decision per click: a double-click used to reach the server twice.
    form.querySelectorAll('button[name="action"]').forEach((button) => { button.disabled = true; });
    if (event.submitter) {
      const hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = 'action';
      hidden.value = event.submitter.value;
      form.appendChild(hidden);
    }
  });
})();
