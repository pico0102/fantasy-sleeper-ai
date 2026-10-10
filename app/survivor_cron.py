import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
import logging
import asyncio

load_dotenv()
log = logging.getLogger(__name__)

# Config
SENDER = os.getenv("GMAIL_SENDER", "pico2014@gmail.com")
PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
RECIPIENT = "pico0102@gmail.com"

async def get_survivor_recommendation_data() -> dict:
    from app.survivor_engine import build_survivor_recommendation
    api_key = os.getenv("GEMINI_API_KEY", "")
    
    prompt = "Give me the Tuesday look-ahead for the Survivor pool. Format it neatly with HTML tags (no markdown, just <h2>, <p>, <ul>, <b>). Include a primary pick, alternate, and confidence."
    return await build_survivor_recommendation(api_key, custom_prompt=prompt)

def generate_html_email(body_content: str) -> str:
    return f"""
    <html>
      <head>
        <style>
          body {{ font-family: -apple-system, sans-serif; line-height: 1.6; color: #333; }}
          .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
          .header {{ background-color: #ea580c; color: white; padding: 15px; border-radius: 8px 8px 0 0; text-align: center; }}
          .content {{ padding: 20px; background: #fafafa; border: 1px solid #eee; border-radius: 0 0 8px 8px; }}
          .confidence-bar {{ background: #eee; height: 10px; border-radius: 5px; overflow: hidden; margin-top: 5px; }}
          .confidence-fill {{ background: #22c55e; height: 100%; width: 85%; }}
          h2 {{ color: #c2410c; }}
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h1 style="margin:0;">🏈 Fat Crew Survivor 🏈</h1>
            <p style="margin:5px 0 0 0;">AI Pick 'em Recommendations</p>
          </div>
          <div class="content">
            {body_content}
          </div>
        </div>
      </body>
    </html>
    """

def send_email(subject: str, html_body: str):
    if not PASSWORD or PASSWORD == "your-16-char-password-here":
        log.warning("GMAIL_APP_PASSWORD not set. DRY RUN MODE.")
        print("--- EMAIL DRY RUN ---")
        print("Subject: " + subject)
        print(html_body)
        print("---------------------")
        return

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = SENDER
    msg['To'] = RECIPIENT

    part = MIMEText(html_body, 'html')
    msg.attach(part)

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(SENDER, PASSWORD)
        server.sendmail(SENDER, RECIPIENT, msg.as_string())
        server.quit()
        log.info(f"Email sent successfully to {RECIPIENT}")
    except Exception as e:
        log.error(f"Failed to send email: {e}")

async def main():
    logging.basicConfig(level=logging.INFO)
    log.info("Generating survivor recommendation...")
    data = await get_survivor_recommendation_data()
    recommendation_html = data.get("recommendation", "")
    week = data.get("week", 1)

    if not recommendation_html or "⚠️" in recommendation_html or "temporarily unavailable" in recommendation_html:
        log.error(f"Cannot send email: recommendation generation failed ({recommendation_html})")
        return

    full_html = generate_html_email(recommendation_html)
    subject = f"Fat Crew Survivor: Week {week} AI Picks"
    send_email(subject, full_html)

if __name__ == "__main__":
    asyncio.run(main())
