from datetime import datetime, timezone
import hmac
import html
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.subscription import SubscriptionRequest, SubscriptionStatus

router = APIRouter(prefix="/admin", tags=["Admin"])
security = HTTPBasic()


def verify_admin_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """
    Validates admin credentials using constant-time comparison against ADMIN_PASSWORD.
    Rejects unauthorized access with HTTP 401 and WWW-Authenticate header.
    """
    correct_password = settings.ADMIN_PASSWORD
    if not credentials or not credentials.password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="StockPing Admin"'},
        )

    # Validate password using constant-time comparison
    is_valid = hmac.compare_digest(credentials.password.encode("utf-8"), correct_password.encode("utf-8"))
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid administrative credentials",
            headers={"WWW-Authenticate": 'Basic realm="StockPing Admin"'},
        )

    return credentials.username


def format_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


@router.get("", response_class=HTMLResponse, summary="Admin subscription management dashboard")
async def get_admin_dashboard(
    username: str = Depends(verify_admin_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Password-protected admin interface displaying customer subscription statuses,
    notification timestamps, delivery retry counts, and error diagnostics.
    """
    stmt = select(SubscriptionRequest).order_by(SubscriptionRequest.created_at.desc())
    subscriptions = (await db.execute(stmt)).scalars().all()

    total_count = len(subscriptions)
    notified_count = sum(1 for s in subscriptions if s.status == SubscriptionStatus.NOTIFIED.value)
    pending_count = sum(1 for s in subscriptions if s.status == SubscriptionStatus.PENDING.value)
    processing_count = sum(1 for s in subscriptions if s.status == SubscriptionStatus.PROCESSING.value)
    failed_count = sum(1 for s in subscriptions if s.status == SubscriptionStatus.FAILED.value)

    rows_html = ""
    if subscriptions:
        for sub in subscriptions:
            status_badge_class = {
                SubscriptionStatus.NOTIFIED.value: "badge-success",
                SubscriptionStatus.PROCESSING.value: "badge-warning",
                SubscriptionStatus.PENDING.value: "badge-neutral",
                SubscriptionStatus.FAILED.value: "badge-danger",
            }.get(sub.status, "badge-neutral")

            safe_email = html.escape(sub.email)
            safe_sku = html.escape(sub.sku)
            safe_status = html.escape(sub.status)
            created_str = format_dt(sub.created_at)
            notified_str = format_dt(sub.notified_at)
            attempts_str = str(sub.attempts)
            error_str = html.escape(sub.last_error or "—")

            rows_html += f"""
            <tr>
              <td>{sub.id}</td>
              <td class="font-mono">{safe_email}</td>
              <td class="font-mono font-bold">{safe_sku}</td>
              <td><span class="badge {status_badge_class}">{safe_status}</span></td>
              <td class="text-muted text-sm">{created_str}</td>
              <td class="text-sm">{notified_str}</td>
              <td class="text-center">{attempts_str}</td>
              <td class="text-muted text-sm max-w-error" title="{error_str}">{error_str}</td>
            </tr>
            """
    else:
        rows_html = """
        <tr>
          <td colspan="8" class="empty-state">
            No customer subscription requests recorded yet.
          </td>
        </tr>
        """

    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>StockPing — Admin Dashboard</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background-color: #f8fafc;
      color: #0f172a;
      line-height: 1.5;
      padding: 24px;
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 24px;
      padding-bottom: 16px;
      border-bottom: 1px solid #e2e8f0;
    }}
    .title-group h1 {{ font-size: 20px; font-weight: 700; color: #0f172a; }}
    .title-group p {{ font-size: 13px; color: #64748b; }}
    .user-pill {{
      font-size: 12px;
      background: #e2e8f0;
      padding: 4px 10px;
      border-radius: 9999px;
      color: #334155;
    }}
    .metrics-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }}
    .metric-card {{
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      padding: 14px;
    }}
    .metric-label {{ font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 600; }}
    .metric-value {{ font-size: 22px; font-weight: 700; color: #0f172a; margin-top: 4px; }}
    .table-container {{
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 13px;
    }}
    th {{
      background: #f8fafc;
      padding: 10px 14px;
      font-weight: 600;
      color: #475569;
      border-bottom: 1px solid #e2e8f0;
      white-space: nowrap;
    }}
    td {{
      padding: 12px 14px;
      border-bottom: 1px solid #f1f5f9;
      vertical-align: middle;
    }}
    tr:hover td {{ background-color: #f8fafc; }}
    .font-mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    .font-bold {{ font-weight: 600; }}
    .text-sm {{ font-size: 12px; }}
    .text-muted {{ color: #64748b; }}
    .text-center {{ text-align: center; }}
    .max-w-error {{ max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }}
    .badge-success {{ background: #dcfce7; color: #166534; }}
    .badge-warning {{ background: #fef3c7; color: #92400e; }}
    .badge-neutral {{ background: #f1f5f9; color: #475569; }}
    .badge-danger {{ background: #fee2e2; color: #991b1b; }}
    .empty-state {{
      text-align: center;
      padding: 40px !important;
      color: #64748b;
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <div class="header">
    <div class="title-group">
      <h1>StockPing Management</h1>
      <p>Back-in-stock subscriptions and delivery audit log</p>
    </div>
    <div class="user-pill">Admin: {html.escape(username)}</div>
  </div>

  <div class="metrics-grid">
    <div class="metric-card">
      <div class="metric-label">Total Requests</div>
      <div class="metric-value">{total_count}</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Pending</div>
      <div class="metric-value">{pending_count}</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Processing</div>
      <div class="metric-value">{processing_count}</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Notified</div>
      <div class="metric-value">{notified_count}</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Failed</div>
      <div class="metric-value">{failed_count}</div>
    </div>
  </div>

  <div class="table-container">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Customer Email</th>
          <th>Product SKU</th>
          <th>Status</th>
          <th>Subscribed At</th>
          <th>Notified At</th>
          <th class="text-center">Attempts</th>
          <th>Diagnostics / Error</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>
</body>
</html>"""

    response = HTMLResponse(content=content, status_code=200)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none';"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/api/subscriptions", summary="JSON API for subscription audit records")
async def get_admin_subscriptions_api(
    username: str = Depends(verify_admin_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Protected JSON endpoint returning subscription records for admin tooling.
    """
    stmt = select(SubscriptionRequest).order_by(SubscriptionRequest.created_at.desc())
    subscriptions = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": s.id,
            "email": s.email,
            "sku": s.sku,
            "status": s.status,
            "created_at": s.created_at,
            "notified_at": s.notified_at,
            "attempts": s.attempts,
            "last_error": s.last_error,
        }
        for s in subscriptions
    ]
