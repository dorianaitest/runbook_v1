import os
from fastapi import Header, HTTPException

def check_key(x_api_key: str = Header(None)):
    key = os.getenv("AGENT_API_KEY")
    if not key or x_api_key != key:
        raise HTTPException(status_code=401, detail="invalid api key")
