document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const r = await fetch('/api/login', { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ password: document.getElementById('pw').value }) });
  if (r.ok) location.href = '/';
  else document.getElementById('err').textContent = 'Wrong password';
});
