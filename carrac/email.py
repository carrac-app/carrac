"""
Email service module — sends transactional emails via Brevo SMTP.

Uses stdlib smtplib + email.mime (no new dependencies).
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger('carrac.email')

BREVO_HOST = os.getenv('BREVO_SMTP_HOST', 'smtp-relay.brevo.com')
BREVO_PORT = int(os.getenv('BREVO_SMTP_PORT', '587'))
BREVO_USER = os.getenv('BREVO_SMTP_USER', '')
BREVO_PASS = os.getenv('BREVO_SMTP_PASS', '') or os.getenv('BREVO_SMTP_PASSWORD', '')
# No default sender: an address on a domain you do not control is not a
# usable From. send_email() refuses (loudly) until it is configured.
BREVO_FROM = os.getenv('BREVO_FROM_EMAIL', '')
BREVO_FROM_NAME = os.getenv('BREVO_FROM_NAME', 'Carrac')


def _mask_email(email: str) -> str:
    """Mask email for logging: user@domain.com -> u***r@d***n.com"""
    if not email or '@' not in email:
        return '***'
    local, domain = email.rsplit('@', 1)
    masked_local = local[0] + '***' + local[-1] if len(local) > 1 else '***'
    parts = domain.rsplit('.', 1)
    if len(parts) == 2 and len(parts[0]) > 1:
        masked_domain = parts[0][0] + '***' + parts[0][-1] + '.' + parts[1]
    else:
        masked_domain = domain
    return f"{masked_local}@{masked_domain}"


def send_email(to: str, subject: str, html_body: str, text_body: str = None) -> bool:
    """
    Send an email via Brevo SMTP (STARTTLS on port 587).

    Returns True on success, False on failure.
    """
    if not BREVO_USER or not BREVO_PASS:
        logger.error("[EMAIL] BREVO_SMTP_USER or BREVO_SMTP_PASS not configured")
        return False
    if not BREVO_FROM:
        logger.error("[EMAIL] BREVO_FROM_EMAIL not configured; not sending (set the sender address)")
        return False

    msg = MIMEMultipart('alternative')
    msg['From'] = f'{BREVO_FROM_NAME} <{BREVO_FROM}>'
    msg['To'] = to
    msg['Subject'] = subject

    if text_body:
        msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    try:
        with smtplib.SMTP(BREVO_HOST, BREVO_PORT, timeout=10) as server:
            server.starttls()
            server.login(BREVO_USER, BREVO_PASS)
            server.sendmail(BREVO_FROM, to, msg.as_string())

        logger.info(f"[EMAIL] Sent '{subject}' to {_mask_email(to)}")
        return True

    except smtplib.SMTPException as e:
        logger.error(f"[EMAIL] SMTP error sending to {_mask_email(to)}: {e}")
        return False
    except Exception as e:
        logger.error(f"[EMAIL] Failed sending to {_mask_email(to)}: {e}")
        return False


def send_invite_email(email: str, code: str, expires_days: int = 14) -> bool:
    """Send a beta invite code. Flask-free (builds the register URL from
    Config.SITE_URL) so the standalone wave-release job can call it directly.
    """
    from urllib.parse import urlencode
    from carrac.site_url import site_base_url, site_host
    base = site_base_url()
    register_url = f"{base}/register?{urlencode({'code': code})}"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#000;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#000;padding:40px 0;">
<tr><td align="center">
<table width="480" cellpadding="0" cellspacing="0" style="background:#0a0a0f;border:1px solid #1a1a2e;border-radius:6px;padding:40px;">
<tr><td style="text-align:center;padding-bottom:24px;">
    <span style="font-size:28px;font-weight:700;color:#3fb950;font-family:'JetBrains Mono',Menlo,Consolas,monospace;letter-spacing:0.02em;">[C<sup style="font-size:0.62em;vertical-align:super;line-height:0;">4</sup>]</span>
</td></tr>
<tr><td style="color:#d1d4dc;font-size:15px;line-height:1.6;padding-bottom:20px;">
    You're off the waitlist &mdash; welcome to the Carrac beta. Your invite code is below. It expires in {expires_days} days.
</td></tr>
<tr><td style="text-align:center;padding-bottom:20px;">
    <span style="display:inline-block;padding:10px 20px;background:#111;border:1px dashed #3fb950;border-radius:4px;color:#3fb950;font-family:'JetBrains Mono',Menlo,Consolas,monospace;font-size:22px;letter-spacing:0.15em;font-weight:700;">{code}</span>
</td></tr>
<tr><td style="text-align:center;padding-bottom:24px;">
    <a href="{register_url}" style="display:inline-block;padding:12px 32px;background:#3fb950;color:#000;font-weight:600;font-size:14px;text-decoration:none;border-radius:4px;">Create your account</a>
</td></tr>
<tr><td style="color:#787b86;font-size:12px;line-height:1.5;padding-bottom:16px;">
    The code is pre-filled when you use the button. If it didn't fill, paste {code} into the invite-code field. This invite is for you &mdash; please don't share it.
</td></tr>
<tr><td style="border-top:1px solid #1a1a2e;padding-top:16px;color:#4a4a5a;font-size:11px;">
    [Carrac] &mdash; {site_host()}
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""
    text = (
        f"Welcome to the Carrac beta!\n\n"
        f"Your invite code: {code}\n"
        f"Create your account: {register_url}\n\n"
        f"The code expires in {expires_days} days. This invite is for you — please don't share it."
    )
    return send_email(email, 'Your Carrac beta invite', html, text_body=text)
