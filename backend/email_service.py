"""
Gmail SMTP email service for sending user invitations.
Uses App Passwords (not regular Gmail password).
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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
