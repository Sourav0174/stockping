import os
from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["Embed"])

DIST_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
INDEX_HTML_PATH = os.path.join(DIST_DIR, "index.html")


@router.get("/embed", response_class=HTMLResponse)
async def get_embed_form(sku: str | None = None):
    """
    Serves the back-in-stock subscription form designed to run inside store iframes.
    Specific iframe-compatible security headers are enforced (frame-ancestors *).
    """
    if os.path.exists(INDEX_HTML_PATH):
        with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Back in Stock Notification</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      font-size: 14px;
      color: #1a1a1a;
      background: transparent;
      padding: 12px;
    }}
    .stockping-card {{
      max-width: 440px;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      padding: 16px;
      background: #ffffff;
    }}
    .stockping-title {{ font-size: 15px; font-weight: 600; margin-bottom: 4px; color: #0f172a; }}
    .stockping-subtitle {{ font-size: 13px; color: #64748b; margin-bottom: 12px; }}
    .stockping-field {{ margin-bottom: 10px; }}
    .stockping-label {{ display: block; font-size: 12px; font-weight: 500; margin-bottom: 4px; color: #334155; }}
    .stockping-input {{
      width: 100%;
      height: 36px;
      padding: 0 10px;
      border: 1px solid #cbd5e1;
      border-radius: 4px;
      font-size: 13px;
      color: #0f172a;
      outline: none;
    }}
    .stockping-input:focus {{ border-color: #0f172a; ring: 1px solid #0f172a; }}
    .stockping-btn {{
      width: 100%;
      height: 38px;
      background: #0f172a;
      color: #ffffff;
      border: none;
      border-radius: 4px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      margin-top: 4px;
    }}
    .stockping-btn:hover {{ background: #1e293b; }}
    .stockping-badge {{
      display: inline-block;
      padding: 2px 6px;
      background: #f1f5f9;
      border: 1px solid #e2e8f0;
      border-radius: 4px;
      font-size: 12px;
      font-family: monospace;
      color: #475569;
    }}
  </style>
</head>
<body>
  <div class="stockping-card">
    <h2 class="stockping-title">Notify Me When Available</h2>
    <p class="stockping-subtitle">Enter your email and we'll alert you the moment this item is restocked.</p>
    <form id="stockForm">
      <div class="stockping-field">
        <label class="stockping-label" for="skuInput">Product SKU</label>
        <input class="stockping-input" id="skuInput" name="sku" type="text" value="{sku or ''}" required />
      </div>
      <div class="stockping-field">
        <label class="stockping-label" for="emailInput">Email address</label>
        <input class="stockping-input" id="emailInput" name="email" type="email" placeholder="you@example.com" required />
      </div>
      <button class="stockping-btn" type="submit" id="submitBtn">Notify Me</button>
    </form>
    <div id="statusBox" style="margin-top:12px; font-size:13px; display:none;"></div>
  </div>
  <script>
    const form = document.getElementById('stockForm');
    const statusBox = document.getElementById('statusBox');
    const submitBtn = document.getElementById('submitBtn');
    form.addEventListener('submit', async (e) => {{
      e.preventDefault();
      submitBtn.disabled = true;
      submitBtn.textContent = 'Subscribing...';
      statusBox.style.display = 'none';
      try {{
        const res = await fetch('/api/requests', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            email: document.getElementById('emailInput').value,
            sku: document.getElementById('skuInput').value
          }})
        }});
        const data = await res.json();
        statusBox.style.display = 'block';
        if (res.ok) {{
          statusBox.style.color = '#15803d';
          statusBox.textContent = data.message;
          form.style.display = 'none';
        }} else {{
          statusBox.style.color = '#b91c1c';
          statusBox.textContent = data.detail || 'Subscription failed. Please check your details.';
        }}
      }} catch (err) {{
        statusBox.style.display = 'block';
        statusBox.style.color = '#b91c1c';
        statusBox.textContent = 'Network error. Please try again.';
      }} finally {{
        submitBtn.disabled = false;
        submitBtn.textContent = 'Notify Me';
      }}
    }});
  </script>
</body>
</html>"""

    response = HTMLResponse(content=content, status_code=200)
    # Enable framing inside third-party stores while protecting against clickjacking elsewhere
    response.headers["Content-Security-Policy"] = "frame-ancestors *;"
    # Explicitly do not set X-Frame-Options: DENY or SAMEORIGIN on the iframe endpoint
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
