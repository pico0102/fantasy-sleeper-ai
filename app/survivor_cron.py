"""Weekly Survivor Pool Email Job.

Runs as a Cloud Run Job on Tuesdays to generate and email Anthony
the optimal Survivor pick for the upcoming NFL week.

Enforces strict structural safeguards:
- Pre-send checks on ODDS_API_KEY and GEMINI_API_KEY
- Structural validation on the generated pick
- Hard block on failure tokens to guarantee an error NEVER sends as a pick
- Uses the identical pick generation logic as the chatbot toggle
"""

from __future__ import annotations

import asyncio
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging
import os
import smtplib
from typing import Optional, Tuple

from dotenv import load_dotenv

from app.config import (
    GEMINI_API_KEY,
    GMAIL_APP_PASSWORD,
    GMAIL_SENDER,
    ODDS_API_KEY,
    SURVIVOR_EMAIL_RECIPIENT,
)
from app.survivor_engine import SurvivorPickResult, generate_survivor_recommendation

load_dotenv()
log = logging.getLogger(__name__)

# Config
SENDER: str = os.getenv("GMAIL_SENDER", GMAIL_SENDER)
PASSWORD: str = os.getenv("GMAIL_APP_PASSWORD", GMAIL_APP_PASSWORD)
RECIPIENT: str = SURVIVOR_EMAIL_RECIPIENT

# Prohibited failure tokens that disqualify any content from being emailed as a pick
FAILURE_TOKENS = [
    "⚠️",
    "temporarily unavailable",
    "cannot recommend",
    "llmerror",
    "odds_api_key",
    "429",
    "401 unauthorized",
    "generation failed",
    "no eligible teams",
    "quota exceeded",
    "status: error",
]


def validate_pick_for_email(result: Optional[SurvivorPickResult]) -> Tuple[bool, str]:
    """
    Strict structural gatekeeper.
    Validates that a survivor pick result is authentic, mathematically sound,
    and free of error messages before any email can be sent.
    """
    if result is None:
        return False, "Pick result is None"

    if not result.is_valid:
        return False, f"Result flagged invalid by engine: {result.error}"

    if result.status != "ok":
        return False, f"Status is '{result.status}' (expected 'ok')"

    if not result.primary_team or len(result.primary_team.strip()) < 3:
        return False, "Primary team is blank or missing"

    # Verify team was not already picked in prior weeks
    picked_lower = [p.lower() for p in result.previously_picked]
    if result.primary_team.lower() in picked_lower or result.primary_abbrev.lower() in picked_lower:
        return False, f"Structural violation: {result.primary_team} was already picked in prior weeks"

    # Probability check: primary favorite must have >= 50% win probability
    if result.primary_prob < 0.50:
        return False, f"Win probability ({result.primary_prob*100:.1f}%) is too low for primary favorite"

    # Scan recommendation text and HTML for error tokens
    content_to_check = f"{result.recommendation_text} {result.recommendation_html}".lower()
    for token in FAILURE_TOKENS:
        if token in content_to_check:
            return False, f"Disallowed failure token '{token}' detected in pick content"

    return True, "Valid"


def generate_html_email(body_content: str) -> str:
    return f"""\
    <html>
      <head>
        <style>
          body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1e293b; background-color: #f8fafc; margin: 0; padding: 20px; }}
          .container {{ max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; border: 1px solid #e2e8f0; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); }}
          .header {{ background: linear-gradient(135deg, #ea580c 0%, #c2410c 100%); color: white; padding: 24px 20px; text-align: center; }}
          .header h1 {{ margin: 0; font-size: 22px; font-weight: 800; letter-spacing: -0.025em; }}
          .header p {{ margin: 6px 0 0 0; font-size: 13px; opacity: 0.9; }}
          .content {{ padding: 24px 20px; }}
          h2 {{ color: #c2410c; margin-top: 0; font-size: 18px; }}
          h3 {{ color: #334155; font-size: 15px; margin-top: 18px; }}
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h1>🏈 Fat Crew Survivor 🏈</h1>
            <p>AI GM Weekly Pick 'em Recommendations</p>
          </div>
          <div class="content">
            {body_content}
          </div>
        </div>
      </body>
    </html>
    """


def send_email(subject: str, html_body: str) -> bool:
    password = os.getenv("GMAIL_APP_PASSWORD", PASSWORD).strip()
    sender = os.getenv("GMAIL_SENDER", SENDER).strip()
    recipient = os.getenv("SURVIVOR_EMAIL_RECIPIENT", RECIPIENT).strip()

    if not password or password == "your-16-char-password-here" or password == "test-mock-key":
        log.warning("GMAIL_APP_PASSWORD not set. DRY RUN MODE.")
        print("--- EMAIL DRY RUN ---")
        print(f"Subject: {subject}")
        print(f"To: {recipient}")
        print(html_body[:300] + "...")
        print("---------------------")
        return True

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    part = MIMEText(html_body, "html")
    msg.attach(part)

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())
        server.quit()
        log.info(f"Email sent successfully to {recipient}")
        return True
    except Exception as e:
        log.error(f"Failed to send email via SMTP: {e}")
        return False


async def run_survivor_cron(dry_run: bool = False) -> bool:
    """
    Executes the weekly survivor email workflow with multi-layered safeguards.
    Returns True if a valid email was sent/dry-run, False if blocked by safeguards.
    """
    log.info("Starting weekly survivor pick workflow...")

    # Safeguard Level 1: Pre-send configuration verification
    api_key_odds = os.getenv("ODDS_API_KEY", ODDS_API_KEY).strip()
    if not api_key_odds or api_key_odds == "your-odds-api-key-here" or api_key_odds == "test-mock-key":
        log.error("PRE-SEND SAFEGUARD BLOCKED: ODDS_API_KEY is not set. Weekly survivor email aborted.")
        return False

    api_key_gemini = os.getenv("GEMINI_API_KEY", GEMINI_API_KEY).strip()
    if not api_key_gemini or api_key_gemini == "test-mock-key":
        log.error("PRE-SEND SAFEGUARD BLOCKED: GEMINI_API_KEY is not set. Weekly survivor email aborted.")
        return False

    # Safeguard Level 2: Generate pick via unified engine
    try:
        result = await generate_survivor_recommendation()
    except Exception as e:
        log.error(f"PRE-SEND SAFEGUARD BLOCKED: Exception during pick generation: {e}")
        return False

    # Safeguard Level 3: Structural pick validation
    is_valid, reason = validate_pick_for_email(result)
    if not is_valid:
        log.error(f"SAFEGUARD BLOCKED EMAIL: {reason}. Email will NOT be sent.")
        return False

    # Safeguard Level 4: Email construction
    full_html = generate_html_email(result.recommendation_html)
    subject = f"Fat Crew Survivor: Week {result.week} AI Picks — {result.primary_team}"

    if dry_run:
        log.info(f"DRY RUN SUCCESS: Valid pick generated: {result.primary_team} ({result.primary_prob*100:.1f}%)")
        return True

    return send_email(subject, full_html)


async def main():
    logging.basicConfig(level=logging.INFO)
    success = await run_survivor_cron()
    if not success:
        log.error("Survivor cron job finished with NO email sent due to safety checks.")


if __name__ == "__main__":
    asyncio.run(main())
