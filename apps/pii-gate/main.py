import json
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
from presidio_analyzer.context_aware_enhancers import LemmaContextAwareEnhancer
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import GLiNERRecognizer
from presidio_anonymizer import AnonymizerEngine

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("pii-gate")

SERPER_UPSTREAM = "https://google.serper.dev"
FIRECRAWL_UPSTREAM = "https://api.firecrawl.dev"
SCORE_THRESHOLD = 0.4
LANGUAGE = "de"
DEBUG_LOG = os.environ.get("PII_GATE_DEBUG_LOG", "false").lower() == "true"

# Sofort maskieren:
MASK_ENTITIES = {
    "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "CREDIT_CARD",
    "LOCATION", "ID_NUMBER", "DE_TAX_ID", "DE_SVNR", "DE_ID_CARD",
}
# Shadow-Mode: nur loggen, (noch) nicht maskieren:
LOG_ONLY_ENTITIES = {"PERSON"}

# Personal-/Possessivpronomen (alle Kasus/Genera/Numeri, inkl. Hoeflichkeitsform):
# tragen ohne ihr Antezedens keine PII, werden aber von GLiNER teils als PERSON
# erkannt. Exakte Span-Treffer hiergegen werden aus PERSON-Treffern gefiltert.
PRONOUN_DENYLIST = {
    "ich", "mich", "mir", "meiner",
    "du", "dich", "dir", "deiner",
    "er", "ihn", "ihm",
    "sie", "ihr", "ihnen", "ihrer",
    "es",
    "wir", "uns", "unser",
    "euch", "euer",
    "mein", "meine", "meinen", "meinem", "meines",
    "dein", "deine", "deinen", "deinem", "deines",
    "sein", "seine", "seinen", "seinem", "seines",
    "ihre", "ihren", "ihrem", "ihres",
    "unsere", "unseren", "unsern", "unserem", "unserm", "unserer", "unsrer",
    "unseres", "unsres",
    "eure", "euren", "eurn", "eurem", "eurm", "eures",
}

# GLiNER-Labels -> Presidio-Entities (Labels siehe Modellkarte)
ENTITY_MAPPING = {
    "person": "PERSON",
    "email": "EMAIL_ADDRESS",
    "phone number": "PHONE_NUMBER",
    "mobile phone number": "PHONE_NUMBER",
    "address": "LOCATION",
    "iban": "IBAN_CODE",
    "credit card number": "CREDIT_CARD",
    "passport number": "ID_NUMBER",
    "identity card number": "ID_NUMBER",
    "national id number": "ID_NUMBER",
    "tax identification number": "DE_TAX_ID",
    "social security number": "ID_NUMBER",
    "driver's license number": "ID_NUMBER",
}

analyzer: AnalyzerEngine | None = None
anonymizer: AnonymizerEngine | None = None


def build_analyzer() -> AnalyzerEngine:
    # Presidios Default-NLP-Engine will en_core_web_lg (~560MB, nicht im
    # Image). GLiNER macht die eigentliche Entity-Erkennung, spaCy wird nur
    # für Tokenisierung/Lemmata gebraucht (Context-Scoring, z.B. DE_TAX_ID
    # unten), das kleine deutsche Modell reicht. Siehe Presidio-GLiNER-Sample.
    nlp_engine = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": LANGUAGE, "model_name": "de_core_news_sm"}],
        }
    ).create_engine()
    # Presidio-Default schaut nur 5 Woerter vor und 0 Woerter nach einem
    # Treffer nach Kontextwoertern (LemmaContextAwareEnhancer-Default). In
    # natuerlicher Sprache steht das Kontextwort oft weiter weg, daher hier
    # ein deutlich groesseres Fenster.
    context_enhancer = LemmaContextAwareEnhancer(
        context_prefix_count=15, context_suffix_count=15)
    engine = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=[LANGUAGE],
                             context_aware_enhancer=context_enhancer)
    # spaCy-NER raus (GLiNER übernimmt die Erkennung), Regex-Recognizer
    # (IBAN, CreditCard, Email, Phone) bleiben drin.
    engine.registry.remove_recognizer("SpacyRecognizer")

    gliner = GLiNERRecognizer(
        model_name="urchade/gliner_multi_pii-v1",
        entity_mapping=ENTITY_MAPPING,
        flat_ner=False,
        multi_label=True,
        map_location="cpu",
        supported_language=LANGUAGE,
    )
    engine.registry.add_recognizer(gliner)

    # Beispiel: deutsche Steuer-ID (11 Ziffern, oft 2-3-3-3 gruppiert)
    steuer_id = PatternRecognizer(
        supported_entity="DE_TAX_ID",
        patterns=[Pattern("de_tax_id", r"\b\d{2}\s?\d{3}\s?\d{3}\s?\d{3}\b", 0.3)],
        context=["steuer", "steuer-id", "idnr", "steuernummer",
                 "identifikationsnummer", "steuerliche identifikationsnummer",
                 "finanzamt"],
        supported_language=LANGUAGE,
    )
    engine.registry.add_recognizer(steuer_id)

    # Sozialversicherungsnummer: 2 Bereichsnummer + 6 Geburtsdatum (TTMMJJ)
    # + 1 Anfangsbuchstabe Geburtsname + 2 Seriennummer + 1 Prüfziffer = 12
    # Zeichen, z.B. "65 291058 M 001".
    svnr = PatternRecognizer(
        supported_entity="DE_SVNR",
        patterns=[Pattern(
            "de_svnr",
            r"\b\d{2}\s?\d{6}\s?[A-Za-z]\s?\d{3}\b",
            0.3,
        )],
        context=["sozialversicherungsnummer", "versicherungsnummer", "svnr",
                  "rentenversicherungsnummer", "rentenkasse", "sv-nummer",
                  "deutsche rentenversicherung", "rentennummer"],
        supported_language=LANGUAGE,
    )
    engine.registry.add_recognizer(svnr)

    # Personalausweis-/Reisepassnummer: 9 alphanumerische Zeichen, offizielles
    # Zeichen-Alphabet schließt einige verwechselbare Buchstaben aus (hier
    # bewusst nicht exakt nachgebildet, wie bei DE_TAX_ID oben: niedriger
    # Basis-Score, verlässt sich auf Kontextwörter statt exaktem Format).
    ausweis = PatternRecognizer(
        supported_entity="DE_ID_CARD",
        patterns=[Pattern(
            "de_id_card",
            r"\b(?=[A-Z0-9]{9}\b)(?=.*[A-Z])(?=.*[0-9])[A-Z0-9]{9}\b",
            0.3,
        )],
        context=["personalausweis", "ausweisnummer", "reisepass",
                  "passnummer", "ausweis-nr", "ausweis"],
        supported_language=LANGUAGE,
    )
    engine.registry.add_recognizer(ausweis)
    return engine


def scrub(text: str) -> tuple[str, dict]:
    """Maskiert MASK_ENTITIES, loggt LOG_ONLY_ENTITIES. Gibt (text, stats)."""
    results = analyzer.analyze(text=text, language=LANGUAGE)
    results = [r for r in results if r.score >= SCORE_THRESHOLD]
    results = [
        r for r in results
        if not (r.entity_type == "PERSON"
                and text[r.start:r.end].strip().lower() in PRONOUN_DENYLIST)
    ]
    to_mask = [r for r in results if r.entity_type in MASK_ENTITIES]
    shadow = [r for r in results if r.entity_type in LOG_ONLY_ENTITIES]

    out = text
    if to_mask:
        out = anonymizer.anonymize(text=text, analyzer_results=to_mask).text

    stats = {
        "masked": sorted({r.entity_type for r in to_mask}),
        "shadow": sorted({r.entity_type for r in shadow}),
        "n_masked": len(to_mask),
        "n_shadow": len(shadow),
    }
    if DEBUG_LOG:
        stats["matches"] = [
            {"entity": r.entity_type, "start": r.start, "end": r.end,
             "score": r.score, "value": text[r.start:r.end]}
            for r in results
        ]
    return out, stats


@asynccontextmanager
async def lifespan(app: FastAPI):
    global analyzer, anonymizer
    analyzer = build_analyzer()
    anonymizer = AnonymizerEngine()
    # Warmup, damit der 1. Request nicht langsam ist
    scrub("Warmup: max@example.com, +49 170 1234567")
    log.info('{"event":"ready"}')
    yield


app = FastAPI(lifespan=lifespan)
client = httpx.AsyncClient(timeout=90)

FORWARD_HEADERS = {"x-api-key", "authorization", "content-type"}


def fwd_headers(request: Request) -> dict:
    return {k: v for k, v in request.headers.items()
            if k.lower() in FORWARD_HEADERS}


@app.get("/healthz")
async def healthz():
    return {"ok": analyzer is not None}


class ScrubRequest(BaseModel):
    text: str


@app.post("/scrub")
async def scrub_endpoint(req: ScrubRequest):
    """Genereller Scrub-Endpoint für beliebige Aufrufer (z.B. fastapi-gateway
    vor Chat Completions), unabhängig von Serper/Firecrawl."""
    text, stats = scrub(req.text)
    if DEBUG_LOG:
        log.info('{"event":"scrub","debug":true,"text":%s,"matches":%s,"masked":%s,"shadow":%s}'
                 % (json.dumps(req.text), json.dumps(stats["matches"]),
                    stats["masked"], stats["shadow"]))
    else:
        # NIE den Inhalt loggen, nur Metadaten!
        log.info('{"event":"scrub","masked":%s,"shadow":%s}'
                 % (stats["masked"], stats["shadow"]))
    return {"text": text, "masked": stats["masked"], "shadow": stats["shadow"]}


@app.post("/serper/{path:path}")
async def serper(path: str, request: Request):
    body = await request.json()
    if isinstance(body.get("q"), str):
        raw_q = body["q"]
        body["q"], stats = scrub(raw_q)
        if DEBUG_LOG:
            log.info('{"event":"serper","debug":true,"path":"%s","text":%s,"matches":%s,"masked":%s,"shadow":%s}'
                     % (path, json.dumps(raw_q), json.dumps(stats["matches"]),
                        stats["masked"], stats["shadow"]))
        else:
            # NIE den Query-Inhalt loggen, nur Metadaten!
            log.info('{"event":"serper","path":"%s","masked":%s,"shadow":%s}'
                     % (path, stats["masked"], stats["shadow"]))
    upstream = await client.post(
        f"{SERPER_UPSTREAM}/{path}", json=body, headers=fwd_headers(request))
    return Response(content=upstream.content,
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type"))


@app.api_route("/firecrawl/{path:path}",
               methods=["GET", "POST", "PUT", "DELETE"])
async def firecrawl(path: str, request: Request):
    upstream = await client.request(
        request.method, f"{FIRECRAWL_UPSTREAM}/{path}",
        content=await request.body(),
        headers=fwd_headers(request),
        params=dict(request.query_params))
    log.info('{"event":"firecrawl","path":"%s","status":%s}'
             % (path, upstream.status_code))
    return Response(content=upstream.content,
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type"))
