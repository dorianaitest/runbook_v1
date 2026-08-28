FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1

# Torch als CPU-Build (spart mehrere GB gegenüber Default-Wheel)
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install -r requirements.txt

# Presidios AnalyzerEngine braucht spaCy fuer Tokenisierung/Lemmata (Context-
# Scoring), auch mit entferntem SpacyRecognizer. GLiNER macht die eigentliche
# Erkennung, das kleine deutsche Modell reicht für Tokenisierung.
RUN python -m spacy download de_core_news_sm

# Modell EINMALIG beim Build laden -> liegt danach im Image
RUN python -c "from gliner import GLiNER; \
    GLiNER.from_pretrained('urchade/gliner_multi_pii-v1')"

# Ab jetzt: kein Kontakt mehr zu huggingface.co
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

WORKDIR /app
COPY main.py .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
