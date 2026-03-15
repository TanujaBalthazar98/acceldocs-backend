"""Email service using Resend for transactional emails."""

import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def _get_resend():
    """Lazy-import resend and configure the API key."""
    if not settings.resend_api_key:
        return None
    try:
        import resend
        resend.api_key = settings.resend_api_key
        return resend
    except ImportError:
        logger.warning("resend package not installed — emails will be skipped")
        return None


def send_invitation_email(
    to_email: str,
    inviter_name: str,
    org_name: str,
    role: str,
    invite_link: str,
) -> bool:
    """Send an invitation email. Returns True if sent, False if skipped/failed."""
    resend = _get_resend()
    if not resend:
        logger.info("Email skipped (no Resend API key): invite for %s", to_email)
        return False

    subject = f"You've been invited to join {org_name} on AccelDocs"
    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 560px; margin: 0 auto; padding: 40px 20px;">
      <div style="text-align: center; margin-bottom: 32px;">
        <h1 style="font-size: 24px; font-weight: 700; color: #1a1a1a; margin: 0;">AccelDocs</h1>
      </div>

      <div style="background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 32px;">
        <h2 style="font-size: 20px; font-weight: 600; color: #1a1a1a; margin: 0 0 16px;">
          You're invited!
        </h2>
        <p style="font-size: 15px; color: #4b5563; line-height: 1.6; margin: 0 0 8px;">
          <strong>{inviter_name}</strong> has invited you to join
          <strong>{org_name}</strong> as a <strong>{role}</strong>.
        </p>
        <p style="font-size: 14px; color: #6b7280; line-height: 1.6; margin: 0 0 24px;">
          Click the button below to accept the invitation. This link expires in 7 days.
        </p>

        <div style="text-align: center; margin: 24px 0;">
          <a href="{invite_link}"
             style="display: inline-block; padding: 12px 32px; background: #2dd4bf;
                    color: #ffffff; font-size: 15px; font-weight: 600;
                    text-decoration: none; border-radius: 8px;">
            Accept Invitation
          </a>
        </div>

        <p style="font-size: 12px; color: #9ca3af; margin: 24px 0 0; text-align: center;">
          If the button doesn't work, copy and paste this link:<br/>
          <a href="{invite_link}" style="color: #2dd4bf; word-break: break-all;">{invite_link}</a>
        </p>
      </div>

      <p style="font-size: 12px; color: #9ca3af; text-align: center; margin-top: 24px;">
        You received this email because someone invited you to AccelDocs.
        If you didn't expect this, you can safely ignore it.
      </p>
    </div>
    """

    try:
        resend.Emails.send({
            "from": settings.resend_from_email,
            "to": [to_email],
            "subject": subject,
            "html": html,
        })
        logger.info("Invitation email sent to %s", to_email)
        return True
    except Exception as exc:
        logger.error("Failed to send invitation email to %s: %s", to_email, exc)
        return False
