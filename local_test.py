import os
from google import genai

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

print("Testing WITHOUT safety settings...")
try:
    interaction = client.interactions.create(
        model="gemini-3.7-flash",
        input="Analyze my lineup for Tony Romo's DUI Mugshot",
        store=False
    )
    print("SUCCESS! Output length:", len(interaction.output_text))
except Exception as e:
    print("ERROR without safety settings:", e)

print("\nTesting WITH dict safety settings + type...")
safety_settings = [
    {"type": "safety_setting", "category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"type": "safety_setting", "category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"type": "safety_setting", "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"type": "safety_setting", "category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]
try:
    interaction = client.interactions.create(
        model="gemini-3.7-flash",
        input="Analyze my lineup for Tony Romo's DUI Mugshot",
        safety_settings=safety_settings,
        store=False
    )
    print("SUCCESS! Output length:", len(interaction.output_text))
except Exception as e:
    print("ERROR with dict:", e)
