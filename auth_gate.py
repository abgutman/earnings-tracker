"""Password gate for the Busy Biz site.

Standalone copy of the root auth_gate, customized for Busy Biz:
  - distinct sessionStorage key ('busybiz_auth') so a login here does NOT
    cross-unlock av-tools (both sites share the abgutman.github.io origin)
  - home link points back to the Busy Biz hub, not av-tools
  - PASSWORD_HASH is the SHA-256 of the Busy Biz password (set below)

The plaintext password is never stored here or in the repo — only its hash,
which is what the existing root gate does too.
"""

NOINDEX_META = '<meta name="robots" content="noindex, nofollow">'

# SHA-256 of the Busy Biz password ("biztools2026").
PASSWORD_HASH = "6f9b9af28410ba27d402b20770ef7467287ccac76863972d3491feedc75703ac"

AUTH_GATE_HTML = f"""
<div id="auth-gate" style="position:fixed;inset:0;background:#0d1117;z-index:99999;display:flex;align-items:center;justify-content:center;">
  <div style="background:#161b22;border:1px solid #21262d;padding:40px;border-radius:12px;text-align:center;max-width:360px;width:90%;box-shadow:0 8px 30px rgba(0,0,0,0.6);">
    <div style="font-size:22px;font-weight:800;letter-spacing:2px;color:#e6edf3;margin-bottom:4px;">BUSY BIZ</div>
    <div style="font-size:12px;color:#8b949e;margin-bottom:22px;">Tools to keep biz busy</div>
    <input id="auth-pw" type="password" placeholder="Password" autofocus
      style="width:100%;padding:12px;border:1px solid #30363d;background:#0d1117;color:#e6edf3;border-radius:6px;font-size:15px;margin-bottom:12px;box-sizing:border-box;"
      onkeydown="if(event.key==='Enter')checkPw()">
    <button onclick="checkPw()"
      style="width:100%;padding:12px;background:#3fb950;color:#0d1117;border:none;border-radius:6px;font-size:15px;font-weight:700;cursor:pointer;">
      Enter</button>
    <p id="auth-err" style="color:#f85149;font-size:13px;margin-top:10px;display:none;">Incorrect password</p>
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
    sessionStorage.setItem('busybiz_auth','1');
    document.getElementById('auth-gate').remove();
  }} else {{
    document.getElementById('auth-err').style.display='block';
    document.getElementById('auth-pw').value='';
  }}
}}
if (sessionStorage.getItem('busybiz_auth')==='1') {{
  document.getElementById('auth-gate').remove();
}}
</script>
"""

HOME_LINK = '<a href="https://abgutman.github.io/busy-biz/" style="position:fixed;top:12px;right:12px;z-index:9998;background:#161b22;color:#e6edf3;border:1px solid #30363d;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;box-shadow:0 2px 6px rgba(0,0,0,0.4);">Busy Biz</a>'


def inject_auth(html, home_link=True):
    """Add noindex meta + password gate (and optionally the home link) to an HTML string.

    Pass home_link=False on the hub homepage itself (the link would point to the
    page you're already on).
    """
    html = html.replace("<head>", f"<head>\n{NOINDEX_META}", 1)
    extra = AUTH_GATE_HTML + ("\n" + HOME_LINK if home_link else "")
    html = html.replace("<body>", f"<body>\n{extra}", 1)
    return html
