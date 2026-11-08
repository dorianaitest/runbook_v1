FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

WORKDIR /app
COPY main.py .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
