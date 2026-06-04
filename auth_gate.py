"""Shared password gate for all dashboard HTML pages."""

NOINDEX_META = '<meta name="robots" content="noindex, nofollow">'

PASSWORD_HASH = "c93289ef40f052d7086625037e113760e055ec65d162906acc0d8645fcbbd336"

AUTH_GATE_HTML = f"""
<div id="auth-gate" style="position:fixed;inset:0;background:#1a1a2e;z-index:99999;display:flex;align-items:center;justify-content:center;">
  <div style="background:white;padding:40px;border-radius:12px;text-align:center;max-width:360px;width:90%;box-shadow:0 4px 20px rgba(0,0,0,0.3);">
    <div style="font-size:28px;margin-bottom:20px;">&#128274;</div>
    <input id="auth-pw" type="password" placeholder="Password" autofocus
      style="width:100%;padding:12px;border:2px solid #ddd;border-radius:6px;font-size:15px;margin-bottom:12px;box-sizing:border-box;"
      onkeydown="if(event.key==='Enter')checkPw()">
    <button onclick="checkPw()"
      style="width:100%;padding:12px;background:#1a1a2e;color:white;border:none;border-radius:6px;font-size:15px;font-weight:600;cursor:pointer;">
      Enter</button>
    <p id="auth-err" style="color:#c0392b;font-size:13px;margin-top:10px;display:none;">Incorrect password</p>
  </div>
</div>
<script>
async function sha256(msg) {{
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(msg));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2,'0')).join('');
}}
async function checkPw() {{
  const h = await sha256(document.getElementById('auth-pw').value);
  if (h === '{PASSWORD_HASH}') {{
    sessionStorage.setItem('court_auth','1');
    document.getElementById('auth-gate').remove();
  }} else {{
    document.getElementById('auth-err').style.display='block';
    document.getElementById('auth-pw').value='';
  }}
}}
if (sessionStorage.getItem('court_auth')==='1') {{
  document.getElementById('auth-gate').remove();
}}
</script>
"""


HOME_LINK = '<a href="https://abgutman.github.io/av-tools/" style="position:fixed;top:12px;right:12px;z-index:9998;background:#1a1a2e;color:white;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;box-shadow:0 2px 6px rgba(0,0,0,0.2);">Av\'s Tools Homepage</a>'


def inject_auth(html):
    """Add noindex meta, password gate, and home link to an HTML page string."""
    html = html.replace("<head>", f"<head>\n{NOINDEX_META}", 1)
    html = html.replace("<body>", f"<body>\n{AUTH_GATE_HTML}\n{HOME_LINK}", 1)
    return html
