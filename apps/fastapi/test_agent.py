import httpx
m = "Rechnung doppelt abgebucht"
u = "http://localhost:8001/agent/classify"
r = httpx.post(u, json={"message": m})
print(r.status_code)
print(r.json())
