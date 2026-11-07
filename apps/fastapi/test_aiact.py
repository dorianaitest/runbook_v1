import httpx

# Testet den EU-AI-Act-Agenten ueber die OpenAI-kompatible Route, so wie
# LibreChat ihn aufruft. Vom Host gegen den gemappten Gateway-Port 8001.
usecase = (
    "Wir wollen Bewerbungen automatisch vorsortieren: Ein KI-System bewertet "
    "eingehende Lebenslaeufe und schlaegt der Personalabteilung eine Rangfolge "
    "der Kandidaten vor."
)

url = "http://localhost:8001/v1/chat/completions"
payload = {
    "model": "aiact-einordnung",
    "messages": [{"role": "user", "content": usecase}],
    "stream": False,
}
# Denselben Bearer schicken, den LibreChat sendet (Compliance-Endpoint):
# das Gateway reicht ihn an Modell + Embeddings des Agenten durch.
headers = {"Authorization": "Bearer sk-librechat-admin"}
r = httpx.post(url, json=payload, headers=headers, timeout=180.0)
print(r.status_code)
print(r.json()["choices"][0]["message"]["content"])
