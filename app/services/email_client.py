from typing import Any
import httpx

from app.config import settings


class EmailException(Exception):
    """Base exception for email service errors."""
    pass


class EmailRetryableError(EmailException):
    """Raised for transient 5xx, network, or timeout failures."""
    pass


class EmailRateLimitError(EmailRetryableError):
    """Raised when the email provider responds with HTTP 429."""
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class EmailPermanentError(EmailException):
    """Raised for non-retryable 4xx client errors (e.g., auth, unverified sender)."""
    def __init__(self, status_code: int, message: str):
        super().__init__(f"Permanent email error {status_code}: {message}")
        self.status_code = status_code


def render_restock_email_html(sku: str) -> str:
    """
    Renders a clean, accessible, mobile-friendly HTML notification email.
    Uses restrained typography and strong visual hierarchy.
    Does not invent fictional product links or marketing fluff.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Back in Stock: {sku}</title>
  <style>
    body {{
      margin: 0;
      padding: 0;
      background-color: #f4f4f5;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      color: #18181b;
      -webkit-text-size-adjust: 100%;
    }}
    .email-wrapper {{
      width: 100%;
      background-color: #f4f4f5;
      padding: 32px 16px;
    }}
    .email-container {{
      max-width: 520px;
      margin: 0 auto;
      background-color: #ffffff;
      border: 1px solid #e4e4e7;
      border-radius: 6px;
      padding: 32px 28px;
    }}
    .header-label {{
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #15803d;
      margin-bottom: 8px;
    }}
    .headline {{
      font-size: 20px;
      font-weight: 700;
      line-height: 1.3;
      color: #09090b;
      margin: 0 0 16px 0;
    }}
    .body-copy {{
      font-size: 14px;
      line-height: 1.5;
      color: #3f3f46;
      margin: 0 0 24px 0;
    }}
    .sku-box {{
      background-color: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 4px;
      padding: 12px 16px;
      margin-bottom: 24px;
    }}
    .sku-label {{
      font-size: 11px;
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: #64748b;
      margin-bottom: 4px;
    }}
    .sku-value {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 15px;
      font-weight: 600;
      color: #0f172a;
    }}
    .cta-button {{
      display: inline-block;
      width: 100%;
      box-sizing: border-box;
      background-color: #09090b;
      color: #ffffff !important;
      text-align: center;
      text-decoration: none;
      font-size: 14px;
      font-weight: 600;
      padding: 12px 20px;
      border-radius: 4px;
    }}
    .footer-note {{
      font-size: 12px;
      color: #71717a;
      margin-top: 28px;
      line-height: 1.4;
      border-top: 1px solid #f4f4f5;
      padding-top: 16px;
    }}
  </style>
</head>
<body>
  <div class="email-wrapper">
    <div class="email-container">
      <div class="header-label">Back in Stock</div>
      <h1 class="headline">An item you requested is now available.</h1>
      <p class="body-copy">
        Good news! The product you subscribed to is back in inventory. Quantities may be limited, so we recommend ordering promptly.
      </p>
      
      <div class="sku-box">
        <div class="sku-label">Product SKU</div>
        <div class="sku-value">{sku}</div>
      </div>

      <table width="100%" border="0" cellspacing="0" cellpadding="0">
        <tr>
          <td>
            <a href="#" class="cta-button">View Product &amp; Order</a>
          </td>
        </tr>
      </table>

      <div class="footer-note">
        You received this notification because you subscribed to back-in-stock alerts for SKU {sku}.
      </div>
    </div>
  </div>
</body>
</html>"""


class EmailClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        from_email: str | None = None,
        timeout: float = 10.0,
    ):
        self.base_url = (base_url or settings.RESEND_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.RESEND_API_KEY
        self.from_email = from_email or settings.FROM_EMAIL
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def send_restock_email(self, to_email: str, sku: str) -> dict[str, Any]:
        """
        Submits an email through the Resend-compatible API.
        POST {RESEND_BASE_URL}/emails
        """
        url = f"{self.base_url}/emails"
        payload = {
            "from": self.from_email,
            "to": [to_email],
            "subject": f"Back in Stock: {sku} is now available",
            "html": render_restock_email_html(sku),
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(url, json=payload, headers=self._headers())
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
                raise EmailRetryableError(f"Network error delivering email to {to_email}: {exc}") from exc

            if response.status_code == 429:
                retry_after_str = response.headers.get("Retry-After")
                retry_after = int(retry_after_str) if retry_after_str and retry_after_str.isdigit() else None
                raise EmailRateLimitError(
                    f"Resend rate limit exceeded for {to_email}",
                    retry_after=retry_after,
                )

            if response.status_code >= 500:
                raise EmailRetryableError(
                    f"Resend server error {response.status_code}: {response.text}"
                )

            if response.status_code >= 400:
                raise EmailPermanentError(
                    response.status_code,
                    f"Resend client error {response.status_code}: {response.text}"
                )

            try:
                return response.json()
            except Exception:
                return {"status": "sent"}


email_client = EmailClient()
