"""
Gmail SMTP email service for sending user invitations and operational alerts.
Uses App Passwords (not regular Gmail password).
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")


def send_invite_email(to_email: str, user_name: str, invite_url: str) -> bool:
    """Send an account setup invitation email.

    Returns True on success, False on failure (logs the error).
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        logger.error("GMAIL_ADDRESS or GMAIL_APP_PASSWORD not configured — cannot send invite email")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "You're invited to Newk's Reporting Platform"
    msg["From"] = f"Newk's Reports <{GMAIL_ADDRESS}>"
    msg["To"] = to_email

    plain = (
        f"Hi {user_name},\n\n"
        f"You've been invited to the Newk's Reporting Platform.\n\n"
        f"Set up your account by visiting this link:\n{invite_url}\n\n"
        f"This link expires in 72 hours.\n\n"
        f"— Newk's Reporting Team"
    )

    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:system-ui,-apple-system,sans-serif;background:#f8fafc;">
  <div style="max-width:520px;margin:2rem auto;background:#ffffff;border-radius:12px;border:1px solid #e5e7eb;overflow:hidden;">
    <!-- Header -->
    <div style="background:#C41E3A;padding:1.5rem 2rem;text-align:center;">
      <h1 style="margin:0;color:#ffffff;font-size:1.4rem;font-weight:700;">Newk's Reporting Platform</h1>
    </div>
    <!-- Body -->
    <div style="padding:2rem;">
      <h2 style="margin:0 0 0.5rem;color:#1E3A5F;font-size:1.15rem;">Hi {user_name},</h2>
      <p style="color:#374151;font-size:0.95rem;line-height:1.6;">
        You've been invited to the <strong>Newk's Reporting Platform</strong>.
        Click the button below to set up your password and access your reports.
      </p>
      <div style="text-align:center;margin:1.75rem 0;">
        <a href="{invite_url}"
           style="display:inline-block;background:#C41E3A;color:#ffffff;text-decoration:none;
                  padding:0.75rem 2rem;border-radius:8px;font-weight:700;font-size:1rem;">
          Set Up Your Account
        </a>
      </div>
      <p style="color:#6B7280;font-size:0.85rem;line-height:1.5;">
        This link expires in <strong>72 hours</strong>. If it has expired, ask your administrator
        to resend the invitation.
      </p>
      <p style="color:#9CA3AF;font-size:0.8rem;margin-top:1.5rem;border-top:1px solid #E5E7EB;padding-top:1rem;">
        If the button doesn't work, copy and paste this URL into your browser:<br>
        <a href="{invite_url}" style="color:#C41E3A;word-break:break-all;">{invite_url}</a>
      </p>
    </div>
  </div>
</body>
</html>"""

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())
        logger.info(f"Invite email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send invite email to {to_email}: {e}")
        return False


def send_password_reset_email(to_email: str, user_name: str, reset_url: str) -> bool:
    """Send a password reset email.

    Returns True on success, False on failure (logs the error).
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        logger.error("GMAIL_ADDRESS or GMAIL_APP_PASSWORD not configured — cannot send password reset email")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Reset your Newk's Reporting password"
    msg["From"] = f"Newk's Reports <{GMAIL_ADDRESS}>"
    msg["To"] = to_email

    plain = (
        f"Hi {user_name},\n\n"
        f"We received a request to reset your password for the Newk's Reporting Platform.\n\n"
        f"Reset your password by visiting this link:\n{reset_url}\n\n"
        f"This link expires in 1 hour. If you didn't request a password reset, you can safely ignore this email.\n\n"
        f"— Newk's Reporting Team"
    )

    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:system-ui,-apple-system,sans-serif;background:#f8fafc;">
  <div style="max-width:520px;margin:2rem auto;background:#ffffff;border-radius:12px;border:1px solid #e5e7eb;overflow:hidden;">
    <!-- Header -->
    <div style="background:#C41E3A;padding:1.5rem 2rem;text-align:center;">
      <h1 style="margin:0;color:#ffffff;font-size:1.4rem;font-weight:700;">Newk's Reporting Platform</h1>
    </div>
    <!-- Body -->
    <div style="padding:2rem;">
      <h2 style="margin:0 0 0.5rem;color:#1E3A5F;font-size:1.15rem;">Hi {user_name},</h2>
      <p style="color:#374151;font-size:0.95rem;line-height:1.6;">
        We received a request to reset your password for the <strong>Newk's Reporting Platform</strong>.
        Click the button below to choose a new password.
      </p>
      <div style="text-align:center;margin:1.75rem 0;">
        <a href="{reset_url}"
           style="display:inline-block;background:#C41E3A;color:#ffffff;text-decoration:none;
                  padding:0.75rem 2rem;border-radius:8px;font-weight:700;font-size:1rem;">
          Reset Your Password
        </a>
      </div>
      <p style="color:#6B7280;font-size:0.85rem;line-height:1.5;">
        This link expires in <strong>1 hour</strong>. If you didn't request a password reset,
        you can safely ignore this email — your password won't change.
      </p>
      <p style="color:#9CA3AF;font-size:0.8rem;margin-top:1.5rem;border-top:1px solid #E5E7EB;padding-top:1rem;">
        If the button doesn't work, copy and paste this URL into your browser:<br>
        <a href="{reset_url}" style="color:#C41E3A;word-break:break-all;">{reset_url}</a>
      </p>
    </div>
  </div>
</body>
</html>"""

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())
        logger.info(f"Password reset email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send password reset email to {to_email}: {e}")
        return False


def send_reconciliation_alert_email(
    changes: List[Dict[str, Any]],
    recipients: List[str],
    window_days: int,
    business_date_range: str,
) -> bool:
    """
    Send an operational alert when the nightly reconciliation job detected
    one or more past-day order amendments that crossed the configured
    "large change" thresholds (net sales delta or voided-check delta).

    `changes` is a list of dicts each shaped like:
        {
          "restaurant_name":   "Newk's Jackson",
          "restaurant_guid":   "abc-...",
          "business_date":     "20260524",   # YYYYMMDD
          "net_sales_before":  4218.50,
          "net_sales_after":   4071.25,
          "net_sales_delta":  -147.25,
          "check_count_before": 142,
          "check_count_after":  138,
          "voided_delta":       4,
        }

    Sends ONE summary email to all recipients rather than one per change.
    """
    if not changes:
        return True  # No-op is a success
    if not recipients:
        logger.warning("Reconciliation alert: no recipients configured, skipping send")
        return False
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        logger.error("GMAIL_ADDRESS or GMAIL_APP_PASSWORD not configured — cannot send reconciliation alert")
        return False

    subject = f"[Newk's Reports] {len(changes)} past-day sales amendment{'s' if len(changes) != 1 else ''} detected"

    # Plain-text body for clients that won't render HTML
    plain_lines = [
        f"The nightly reconciliation job re-pulled {window_days} days of orders ({business_date_range})",
        f"and detected {len(changes)} (store, date) pairs where the data changed since the last pull.",
        "",
        "Most likely explanation: the restaurant voided or amended an order from a past business date.",
        "The local DB has been updated to match Toast — this email is just a heads-up so you can",
        "spot-check anything material.",
        "",
        f"{'Store':<25} {'Date':<12} {'Net Sales Δ':>14} {'Check Δ':>10} {'Voided Δ':>10}",
        "-" * 75,
    ]
    for c in changes:
        d = c.get("business_date", "")
        date_pretty = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        plain_lines.append(
            f"{(c.get('restaurant_name') or '')[:25]:<25} "
            f"{date_pretty:<12} "
            f"${c.get('net_sales_delta', 0):>+12,.2f} "
            f"{c.get('check_count_after', 0) - c.get('check_count_before', 0):>+10} "
            f"{c.get('voided_delta', 0):>+10}"
        )
    plain_lines.append("")
    plain_lines.append("— Newk's Reporting Platform")
    plain = "\n".join(plain_lines)

    # HTML body — table of changes, sortable visually
    rows_html = []
    for c in changes:
        d = c.get("business_date", "")
        date_pretty = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        ns_delta = c.get("net_sales_delta", 0)
        ns_color = "#16a34a" if ns_delta > 0 else ("#dc2626" if ns_delta < 0 else "#6b7280")
        check_delta = c.get("check_count_after", 0) - c.get("check_count_before", 0)
        voided_delta = c.get("voided_delta", 0)
        rows_html.append(f"""
          <tr>
            <td style="padding:0.5rem 0.75rem;border-bottom:1px solid #e5e7eb;">{c.get('restaurant_name', '')}</td>
            <td style="padding:0.5rem 0.75rem;border-bottom:1px solid #e5e7eb;font-family:monospace;">{date_pretty}</td>
            <td style="padding:0.5rem 0.75rem;border-bottom:1px solid #e5e7eb;text-align:right;">
              ${c.get('net_sales_before', 0):,.2f} → ${c.get('net_sales_after', 0):,.2f}
            </td>
            <td style="padding:0.5rem 0.75rem;border-bottom:1px solid #e5e7eb;text-align:right;font-weight:600;color:{ns_color};">
              ${ns_delta:+,.2f}
            </td>
            <td style="padding:0.5rem 0.75rem;border-bottom:1px solid #e5e7eb;text-align:right;">
              {check_delta:+d}
            </td>
            <td style="padding:0.5rem 0.75rem;border-bottom:1px solid #e5e7eb;text-align:right;">
              {voided_delta:+d}
            </td>
          </tr>""")

    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:system-ui,-apple-system,sans-serif;background:#f8fafc;">
  <div style="max-width:780px;margin:2rem auto;background:#ffffff;border-radius:12px;border:1px solid #e5e7eb;overflow:hidden;">
    <div style="background:#C41E3A;padding:1.25rem 2rem;color:#ffffff;">
      <h1 style="margin:0;font-size:1.25rem;font-weight:700;">Past-day sales reconciliation</h1>
      <p style="margin:0.25rem 0 0;opacity:0.9;font-size:0.9rem;">{len(changes)} (store, date) pairs changed since the last pull · window {business_date_range}</p>
    </div>
    <div style="padding:1.5rem 2rem;">
      <p style="color:#374151;font-size:0.95rem;line-height:1.6;margin-top:0;">
        The nightly reconciliation job re-pulled the last <strong>{window_days} days</strong> of orders
        and noticed material differences vs. the previous pull. The local database has been updated
        to match Toast — this is a heads-up so you can spot-check anything noteworthy.
      </p>
      <p style="color:#6b7280;font-size:0.85rem;line-height:1.6;">
        Most likely cause: the restaurant voided or amended an order from a past business date
        (refund, item add/remove, retroactive discount, etc.).
      </p>
      <table style="width:100%;border-collapse:collapse;margin-top:1rem;font-size:0.875rem;">
        <thead>
          <tr style="background:#f9fafb;color:#374151;text-align:left;">
            <th style="padding:0.5rem 0.75rem;border-bottom:2px solid #e5e7eb;">Store</th>
            <th style="padding:0.5rem 0.75rem;border-bottom:2px solid #e5e7eb;">Business Date</th>
            <th style="padding:0.5rem 0.75rem;border-bottom:2px solid #e5e7eb;text-align:right;">Net Sales (before → after)</th>
            <th style="padding:0.5rem 0.75rem;border-bottom:2px solid #e5e7eb;text-align:right;">Δ Net</th>
            <th style="padding:0.5rem 0.75rem;border-bottom:2px solid #e5e7eb;text-align:right;">Δ Checks</th>
            <th style="padding:0.5rem 0.75rem;border-bottom:2px solid #e5e7eb;text-align:right;">Δ Voided</th>
          </tr>
        </thead>
        <tbody>{''.join(rows_html)}</tbody>
      </table>
      <p style="color:#9ca3af;font-size:0.8rem;margin-top:1.5rem;border-top:1px solid #e5e7eb;padding-top:1rem;">
        Generated by the nightly reconciliation job. Thresholds and recipients are configured via
        RECONCILE_* environment variables.
      </p>
    </div>
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Newk's Reports <{GMAIL_ADDRESS}>"
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())
        logger.info(
            "Reconciliation alert sent to %d recipient(s) covering %d changes",
            len(recipients), len(changes),
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send reconciliation alert email: {e}")
        return False
