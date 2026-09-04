import requests

url = "https://fantasy-ai-service-519501469706.us-east1.run.app/api/chat"
headers = {"Content-Type": "application/json"}
data = {"query": "Analyze my lineup for this week and tell me who to start."}

response = requests.post(url, headers=headers, json=data)
print("STATUS:", response.status_code)
if response.status_code == 200:
    res = response.json()
    print("ANSWER LENGTH:", len(res.get("answer", "")))
    print("ANSWER:", repr(res.get("answer", "")))
else:
    print("ERROR:", response.text)
