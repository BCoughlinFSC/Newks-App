"""
SMTP email service for user invitations and operational alerts.

Transport is env-driven so it can be re-pointed at any relay without code
changes. Defaults to the Exchange Online relay used after the 2026-06 Google ->
Microsoft cutover (no-auth, IP-allowlisted, port 25, any @newks.com sender).
To use authenticated submission instead (Gmail / O365), set SMTP_USER +
SMTP_PASSWORD (+ SMTP_SECURITY).

NOTE: the no-auth Exchange relay only accepts mail from IPs allowlisted on the
connector — this host's public IP must be on Adam's list, or sends will be
rejected. (Authenticated submission is not IP-restricted.)
"""

import logging
import os
import smtplib
import ssl
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# --- SMTP transport (env-driven) ---------------------------------------------
SMTP_HOST      = os.getenv("SMTP_HOST", "newks-com.mail.protection.outlook.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "25"))
SMTP_FROM      = os.getenv("SMTP_FROM", "reports@newks.com")   # any @newks.com address
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Newk's Reports")
SMTP_USER      = os.getenv("SMTP_USER", "")          # blank => no auth (the relay)
SMTP_PASSWORD  = os.getenv("SMTP_PASSWORD", "")
# Transport security: "none" (plain submission — the relay) · "starttls" · "ssl"
SMTP_SECURITY  = os.getenv("SMTP_SECURITY", "starttls" if os.getenv("SMTP_USER") else "none").lower()

# Back-compat: if the legacy GMAIL_* vars are set and SMTP_HOST has NOT been
# explicitly overridden, keep sending via authenticated Gmail so nothing breaks
# mid-migration. Set SMTP_HOST (or clear GMAIL_*) to switch to the relay.
_GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
_GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
if _GMAIL_ADDRESS and _GMAIL_APP_PASSWORD and not os.getenv("SMTP_HOST"):
    SMTP_HOST = "smtp.gmail.com"
    SMTP_PORT = 465
    SMTP_SECURITY = "ssl"
    SMTP_USER = _GMAIL_ADDRESS
    SMTP_PASSWORD = _GMAIL_APP_PASSWORD
    SMTP_FROM = _GMAIL_ADDRESS

FROM_HEADER = f"{SMTP_FROM_NAME} <{SMTP_FROM}>"


def _send(msg: MIMEMultipart, recipients) -> bool:
    """Send a prepared message via the configured SMTP transport.
    Returns True on success, False on failure (logged). Never raises."""
    if isinstance(recipients, str):
        recipients = [recipients]
    if not SMTP_HOST:
        logger.error("SMTP_HOST not configured — cannot send email")
        return False
    try:
        if SMTP_SECURITY == "ssl":
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        with server:
            server.ehlo()
            if SMTP_SECURITY == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, recipients, msg.as_string())
        return True
    except Exception as e:
        logger.error("SMTP send to %s failed: %s", recipients, e)
        return False


def send_plain_email(to, subject: str, body: str, cc=None) -> bool:
    """Send a plain-text email via the configured transport (O365 / relay / Gmail).

    `to` and `cc` may be a str or a list of addresses. The From header is always the
    configured SMTP_FROM (e.g. reports@newks.com) so authenticated O365 submission is
    accepted. Returns True on success, False on failure (logged). Never raises.
    """
    to_list = [to] if isinstance(to, str) else [a for a in to if a]
    cc_list = [] if not cc else ([cc] if isinstance(cc, str) else [a for a in cc if a])
    if not to_list:
        logger.error("send_plain_email: no recipients")
        return False
    if not SMTP_HOST:
        logger.error("SMTP not configured — cannot send email")
        return False

    msg = MIMEMultipart()
    msg["From"] = FROM_HEADER
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    # Envelope recipients include Cc so they actually receive it.
    return _send(msg, to_list + cc_list)


def send_report_email(to, subject: str, html_body: str, cc=None,
                      attachments=None, bcc=None, reply_to: str = "") -> bool:
    """Send a subscribed report: HTML body plus optional file attachments.

    `attachments` is a list of (filename, bytes, mime_subtype) tuples — e.g.
    ("Refunds.xlsx", b"...", "vnd.openxmlformats-officedocument.spreadsheetml.sheet").
    Used by the report-subscription dispatcher (see report_email.py).

    `bcc` recipients get the mail without appearing in any header — every SSRS
    subscription these replace BCC'd reports@newks.com to keep an archive copy,
    so bcc addresses go into the SMTP envelope only. `reply_to` sets Reply-To
    (SSRS used itdepartment@newks.com) so replies don't land on the send-only
    reports@ mailbox.

    Returns True on success, False on failure (logged). Never raises.
    """
    to_list  = [to] if isinstance(to, str) else [a for a in (to or []) if a]
    cc_list  = [] if not cc  else ([cc]  if isinstance(cc, str)  else [a for a in cc  if a])
    bcc_list = [] if not bcc else ([bcc] if isinstance(bcc, str) else [a for a in bcc if a])
    if not to_list:
        logger.error("send_report_email: no recipients for %r", subject)
        return False

    msg = MIMEMultipart("mixed")
    msg["From"] = FROM_HEADER
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    for filename, blob, subtype in (attachments or []):
        part = MIMEBase("application", subtype)
        part.set_payload(blob)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    # Bcc is envelope-only — deliberately never written into a header.
    return _send(msg, to_list + cc_list + bcc_list)


def send_invite_email(to_email: str, user_name: str, invite_url: str) -> bool:
    """Send an account setup invitation email.

    Returns True on success, False on failure (logs the error).
    """
    if not SMTP_HOST:
        logger.error("SMTP not configured — cannot send invite email")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "You're invited to Newk's Reporting Platform"
    msg["From"] = FROM_HEADER
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

    if _send(msg, to_email):
        logger.info(f"Invite email sent to {to_email}")
        return True
    logger.error(f"Failed to send invite email to {to_email}")
    return False


def send_onboarding_invite_email(to_email: str, franchisee_name: str,
                                 store_label: str, journey_url: str) -> bool:
    """Invite a franchisee to their NRO IT Onboarding Journey.

    `store_label` is the location line (e.g. "Newk's Eatery — Franklin, TN").
    `journey_url` is the magic-link URL (carries the per-opening token; no
    password needed). Returns True on success, False on failure (logged).
    """
    if not SMTP_HOST:
        logger.error("SMTP not configured — cannot send onboarding invite")
        return False

    name = franchisee_name or "there"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your Newk's Onboarding Journey — {store_label}"
    msg["From"] = FROM_HEADER
    msg["To"] = to_email

    plain = (
        f"Hi {name},\n\n"
        f"Welcome to the Newk's family! Your IT onboarding journey for "
        f"{store_label} is ready.\n\n"
        f"Open your personal checklist here (no password needed):\n{journey_url}\n\n"
        f"You'll see every setup task in the order we recommend tackling it, with "
        f"links to step-by-step Knowledge Base articles. Check items off as you go "
        f"and your progress saves automatically — and the FSC IT team sees it too, "
        f"so we can review together on your 90/60/30-day status calls.\n\n"
        f"Keep this email — the link is your way back in any time.\n\n"
        f"— FSC Shared Services IT Team"
    )

    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:system-ui,-apple-system,sans-serif;background:#F1ECE4;">
  <div style="max-width:560px;margin:2rem auto;background:#ffffff;border-radius:12px;border:1px solid #E4DBCD;overflow:hidden;">
    <div style="background:#9E1B32;padding:1.75rem 2rem;">
      <div style="font-family:Georgia,serif;font-size:11px;letter-spacing:2.5px;text-transform:uppercase;color:#F3C9CF;">Newk's Eatery &middot; IT Onboarding</div>
      <h1 style="margin:0.4rem 0 0;color:#ffffff;font-size:1.5rem;font-weight:normal;font-family:Georgia,serif;">Welcome to the Newk's Family</h1>
    </div>
    <div style="height:4px;background:#C9A24B;"></div>
    <div style="padding:2rem;">
      <h2 style="margin:0 0 0.5rem;color:#2A2320;font-size:1.1rem;">Hi {name},</h2>
      <p style="color:#3A322D;font-size:0.95rem;line-height:1.6;">
        Your IT onboarding journey for <strong>{store_label}</strong> is ready. It's your
        complete setup checklist — in the order we recommend tackling it, with links to a
        step-by-step article for every task.
      </p>
      <div style="text-align:center;margin:1.75rem 0;">
        <a href="{journey_url}"
           style="display:inline-block;background:#9E1B32;color:#ffffff;text-decoration:none;
                  padding:0.8rem 2rem;border-radius:8px;font-weight:700;font-size:1rem;">
          Open Your Onboarding Journey
        </a>
      </div>
      <p style="color:#5C534B;font-size:0.9rem;line-height:1.6;">
        No password needed — this link is your way in. Check items off as you go; your
        progress saves automatically and the FSC IT team can see it too, so we can review
        together on your 90 / 60 / 30-day status calls.
      </p>
      <p style="color:#9CA3AF;font-size:0.8rem;margin-top:1.5rem;border-top:1px solid #EDE4D5;padding-top:1rem;">
        Keep this email — the link brings you back any time. If the button doesn't work,
        copy and paste this URL:<br>
        <a href="{journey_url}" style="color:#9E1B32;word-break:break-all;">{journey_url}</a>
      </p>
    </div>
  </div>
</body>
</html>"""

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    if _send(msg, to_email):
        logger.info(f"Onboarding invite sent to {to_email} for {store_label}")
        return True
    logger.error(f"Failed to send onboarding invite to {to_email}")
    return False


def send_password_reset_email(to_email: str, user_name: str, reset_url: str) -> bool:
    """Send a password reset email.

    Returns True on success, False on failure (logs the error).
    """
    if not SMTP_HOST:
        logger.error("SMTP not configured — cannot send password reset email")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Reset your Newk's Reporting password"
    msg["From"] = FROM_HEADER
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

    if _send(msg, to_email):
        logger.info(f"Password reset email sent to {to_email}")
        return True
    logger.error(f"Failed to send password reset email to {to_email}")
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
    if not SMTP_HOST:
        logger.error("SMTP not configured — cannot send reconciliation alert")
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
    msg["From"] = FROM_HEADER
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    if _send(msg, recipients):
        logger.info(
            "Reconciliation alert sent to %d recipient(s) covering %d changes",
            len(recipients), len(changes),
        )
        return True
    logger.error("Failed to send reconciliation alert email")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# NRO Onboarding notifications
#
# Two shapes, deliberately different in urgency:
#   • A question means a franchisee is blocked — sent the instant it is asked.
#   • Completions are progress, not events — batched into one end-of-day digest
#     so an active build doesn't bury the inbox.
# Recipients default to the NRO owners and are overridable per environment.
# ─────────────────────────────────────────────────────────────────────────────

import html as _html

NRO_NOTIFY_TO = [
    e.strip() for e in os.getenv(
        "NRO_NOTIFY_TO", "kaylesworth@newks.com,bcoughlin@fscfranchiseco.com"
    ).split(",") if e.strip()
]


def _esc(s) -> str:
    """Escape user-supplied text before it goes into an HTML email body."""
    return _html.escape(str(s or ""), quote=False)


_SHELL_OPEN = """\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:system-ui,-apple-system,sans-serif;background:#F1ECE4;">
  <div style="max-width:600px;margin:2rem auto;background:#ffffff;border-radius:12px;border:1px solid #E4DBCD;overflow:hidden;">
    <div style="background:%(bar)s;padding:1.5rem 2rem;">
      <div style="font-family:Georgia,serif;font-size:11px;letter-spacing:2.5px;text-transform:uppercase;color:%(eyebrow)s;">Newk's Eatery &middot; NRO Onboarding</div>
      <h1 style="margin:0.4rem 0 0;color:#ffffff;font-size:1.35rem;font-weight:normal;font-family:Georgia,serif;">%(heading)s</h1>
    </div>
    <div style="height:4px;background:#C9A24B;"></div>
    <div style="padding:1.75rem 2rem;">
"""

_SHELL_CLOSE = """\
    </div>
  </div>
</body>
</html>"""


def _shell(heading: str, body_html: str, bar: str = "#9E1B32",
           eyebrow: str = "#F3C9CF") -> str:
    return (_SHELL_OPEN % {"bar": bar, "eyebrow": eyebrow, "heading": _esc(heading)}
            + body_html + _SHELL_CLOSE)


def send_task_question_email(store_label: str, task_title: str, question: str,
                             asked_by: str, dashboard_url: str,
                             recipients=None) -> bool:
    """A franchisee has asked about a task — notify the NRO owners immediately.
    `dashboard_url` deep-links to the opening so it can be answered in one click."""
    if not SMTP_HOST:
        logger.error("SMTP not configured - cannot send NRO question notification")
        return False
    to = list(recipients or NRO_NOTIFY_TO)
    if not to:
        logger.warning("NRO_NOTIFY_TO is empty - question notification not sent")
        return False

    who = asked_by or "The franchisee"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[NRO Question] {store_label} - {task_title[:70]}"
    msg["From"] = FROM_HEADER
    msg["To"] = ", ".join(to)

    plain = (
        f"{who} has a question about an onboarding task.\n\n"
        f"Opening : {store_label}\n"
        f"Task    : {task_title}\n\n"
        f"Question:\n{question}\n\n"
        f"Answer it here: {dashboard_url}\n"
    )
    html = _shell(
        "A franchisee has a question",
        f"""
      <p style="margin:0 0 1rem;color:#3A322D;font-size:0.95rem;line-height:1.6;">
        <strong>{_esc(who)}</strong> has a question about an onboarding task for
        <strong>{_esc(store_label)}</strong>.
      </p>
      <div style="background:#FAF6EF;border:1px solid #EDE4D5;border-left:4px solid #9E1B32;
                  border-radius:8px;padding:1rem 1.15rem;margin:0 0 1.25rem;">
        <div style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#8A7F73;margin-bottom:.35rem;">Task</div>
        <div style="color:#2A2320;font-size:0.95rem;font-weight:600;margin-bottom:.9rem;">{_esc(task_title)}</div>
        <div style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#8A7F73;margin-bottom:.35rem;">Question</div>
        <div style="color:#3A322D;font-size:0.95rem;line-height:1.6;white-space:pre-wrap;">{_esc(question)}</div>
      </div>
      <div style="text-align:center;margin:1.5rem 0 .5rem;">
        <a href="{dashboard_url}" style="display:inline-block;background:#9E1B32;color:#ffffff;
           text-decoration:none;padding:0.75rem 1.75rem;border-radius:8px;font-weight:700;font-size:0.95rem;">
          Answer in the Dashboard
        </a>
      </div>
      <p style="color:#9CA3AF;font-size:0.8rem;margin-top:1.25rem;border-top:1px solid #EDE4D5;padding-top:1rem;">
        Replying to this email won't reach the franchisee - answer in the dashboard and
        they're emailed automatically.
      </p>
""")
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    if _send(msg, to):
        logger.info("NRO question notification sent to %s (%s)", to, store_label)
        return True
    logger.error("Failed to send NRO question notification for %s", store_label)
    return False


def send_question_answered_email(to_email: str, franchisee_name: str,
                                 store_label: str, task_title: str,
                                 question: str, answer: str,
                                 answered_by: str, journey_url: str) -> bool:
    """Reply to the franchisee once their question has been answered."""
    if not SMTP_HOST or not to_email:
        return False
    name = franchisee_name or "there"
    by = answered_by or "The Newk's team"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Re: your question - {task_title[:70]}"
    msg["From"] = FROM_HEADER
    msg["To"] = to_email

    plain = (
        f"Hi {name},\n\n"
        f"{by} has answered your question about \"{task_title}\" for {store_label}.\n\n"
        f"You asked:\n{question}\n\n"
        f"Answer:\n{answer}\n\n"
        f"Back to your checklist: {journey_url}\n\n"
        f"- FSC Shared Services IT Team"
    )
    html = _shell(
        "Your question has been answered",
        f"""
      <p style="margin:0 0 1rem;color:#3A322D;font-size:0.95rem;line-height:1.6;">
        Hi {_esc(name)} &mdash; <strong>{_esc(by)}</strong> has answered your question about
        <strong>{_esc(task_title)}</strong>.
      </p>
      <div style="background:#FAF6EF;border:1px solid #EDE4D5;border-radius:8px;padding:1rem 1.15rem;margin:0 0 .75rem;">
        <div style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#8A7F73;margin-bottom:.35rem;">You asked</div>
        <div style="color:#5C534B;font-size:0.92rem;line-height:1.6;white-space:pre-wrap;">{_esc(question)}</div>
      </div>
      <div style="background:#F2F7F0;border:1px solid #D9E7D3;border-left:4px solid #1E6B34;
                  border-radius:8px;padding:1rem 1.15rem;margin:0 0 1.25rem;">
        <div style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#5E7A58;margin-bottom:.35rem;">Answer</div>
        <div style="color:#2A2320;font-size:0.95rem;line-height:1.6;white-space:pre-wrap;">{_esc(answer)}</div>
      </div>
      <div style="text-align:center;margin:1.5rem 0 .5rem;">
        <a href="{journey_url}" style="display:inline-block;background:#9E1B32;color:#ffffff;
           text-decoration:none;padding:0.75rem 1.75rem;border-radius:8px;font-weight:700;font-size:0.95rem;">
          Back to Your Checklist
        </a>
      </div>
""", bar="#1E6B34", eyebrow="#CFE3C8")
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    if _send(msg, to_email):
        logger.info("NRO answer notification sent to %s (%s)", to_email, store_label)
        return True
    return False


def send_onboarding_digest_email(digests, dashboard_url: str,
                                 recipients=None) -> bool:
    """End-of-day completion summary across every opening that moved today.

    `digests` is the list of per-opening dicts from
    onboarding_db.pending_digest_events().values()."""
    if not SMTP_HOST:
        logger.error("SMTP not configured - cannot send NRO digest")
        return False
    to = list(recipients or NRO_NOTIFY_TO)
    if not to or not digests:
        return False

    total = sum(len(d["events"]) for d in digests)
    msg = MIMEMultipart("alternative")
    noun = "update" if total == 1 else "updates"
    plural = "s" if len(digests) != 1 else ""
    msg["Subject"] = f"[NRO Daily] {total} {noun} across {len(digests)} opening{plural}"
    msg["From"] = FROM_HEADER
    msg["To"] = ", ".join(to)

    plain_parts, html_parts = [], []
    for d in digests:
        place = ", ".join([p for p in [d.get("city"), d.get("state")] if p])
        label = (d.get("store_name") or "Newk's Eatery") + (f" - {place}" if place else "")
        done = [e for e in d["events"] if e["kind"] == "task_done"]
        undone = [e for e in d["events"] if e["kind"] == "task_undone"]
        target = f"  (target open {d['target_open']})" if d.get("target_open") else ""

        plain_parts.append(f"\n{label}{target}")
        for e in done:
            plain_parts.append(f"  [x] {e['detail']}  - {e.get('actor') or 'unknown'}")
        for e in undone:
            plain_parts.append(f"  [ ] re-opened: {e['detail']}  - {e.get('actor') or 'unknown'}")

        rows = "".join(
            '<tr><td style="padding:5px 8px 5px 10px;vertical-align:top;font-size:0.95rem;color:'
            + ("#1E6B34" if e["kind"] == "task_done" else "#B45309") + ';">'
            + ("&#10003;" if e["kind"] == "task_done" else "&#8634;")
            + '</td><td style="padding:5px 10px 5px 0;color:#3A322D;font-size:0.9rem;line-height:1.45;">'
            + _esc(e.get("detail"))
            + '<span style="color:#9CA3AF;"> &mdash; ' + _esc(e.get("actor") or "unknown")
            + "</span></td></tr>"
            for e in done + undone
        )
        sub = f"{len(done)} completed"
        if undone:
            sub += f" &middot; {len(undone)} re-opened"
        if d.get("target_open"):
            sub += f" &middot; target open {_esc(d['target_open'])}"
        html_parts.append(f"""
      <div style="margin:0 0 1.5rem;">
        <div style="font-weight:700;color:#2A2320;font-size:1rem;">{_esc(label)}</div>
        <div style="color:#8A7F73;font-size:0.8rem;margin-bottom:.5rem;">{sub}</div>
        <table style="width:100%;border-collapse:collapse;background:#FAF6EF;border:1px solid #EDE4D5;border-radius:8px;">
          {rows}
        </table>
      </div>""")

    plain = ("Today's NRO onboarding progress:\n" + "\n".join(plain_parts)
             + f"\n\nFull dashboard: {dashboard_url}\n")
    html = _shell(
        "Today's onboarding progress",
        "".join(html_parts) + f"""
      <div style="text-align:center;margin:1.5rem 0 .5rem;">
        <a href="{dashboard_url}" style="display:inline-block;background:#9E1B32;color:#ffffff;
           text-decoration:none;padding:0.75rem 1.75rem;border-radius:8px;font-weight:700;font-size:0.95rem;">
          Open the NRO Dashboard
        </a>
      </div>
      <p style="color:#9CA3AF;font-size:0.8rem;margin-top:1.25rem;border-top:1px solid #EDE4D5;padding-top:1rem;">
        Questions from franchisees are emailed the moment they're asked - this daily
        note covers completed tasks only.
      </p>
""", bar="#203864", eyebrow="#B9C7E2")
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    if _send(msg, to):
        logger.info("NRO digest sent to %s (%d events, %d openings)",
                    to, total, len(digests))
        return True
    logger.error("Failed to send NRO digest")
    return False
