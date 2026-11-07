import os
from typing import Literal
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

# Backend-Modell fuer die Einordnung. Laeuft ueber LiteLLM (sk-librechat-admin),
# ist damit budget- und Langfuse-getrackt wie jeder andere Aufruf.
AIACT_BACKEND_MODEL = os.getenv("AIACT_BACKEND_MODEL", "claude-sonnet-4-6")


class AIActEinordnung(BaseModel):
    """Strukturierte Ersteinschaetzung eines KI-Anwendungsfalls nach
    Verordnung (EU) 2024/1689 (EU AI Act)."""

    risikoklasse: Literal[
        "verboten", "hochrisiko", "begrenztes_risiko", "minimales_risiko"
    ] = Field(
        description="Risikoklasse nach EU AI Act. 'verboten' = Art. 5, "
        "'hochrisiko' = Art. 6 i.V.m. Anhang III oder Anhang I, "
        "'begrenztes_risiko' = nur Transparenzpflichten (Art. 50), "
        "'minimales_risiko' = keine spezifischen Pflichten."
    )
    rolle: Literal[
        "anbieter", "betreiber", "einfuehrer", "haendler", "unklar"
    ] = Field(
        description="Rolle des Nutzers im Sinne des AI Act. 'anbieter' = "
        "entwickelt/laesst entwickeln und bringt in Verkehr; 'betreiber' = "
        "setzt ein KI-System unter eigener Verantwortung ein."
    )
    begruendung: str = Field(
        description="Praezise fachliche Begruendung der Einordnung, mit Bezug "
        "auf die einschlaegigen Artikel/Anhaenge."
    )
    annex_iii_kategorien: list[str] = Field(
        default_factory=list,
        description="Relevante Hochrisiko-Bereiche aus Anhang III, falls "
        "zutreffend (z. B. 'Beschaeftigung/Personalauswahl', 'Bildung', "
        "'Kreditwuerdigkeit'). Leer, wenn nicht hochrisiko.",
    )
    gpai_betroffen: bool = Field(
        description="Ob GPAI-Pflichten (General Purpose AI, Kap. V) relevant "
        "sind, z. B. bei Bereitstellung/Feinabstimmung eines Basismodells."
    )
    transparenzpflichten: list[str] = Field(
        default_factory=list,
        description="Einschlaegige Transparenzpflichten nach Art. 50 "
        "(z. B. Chatbot-Kennzeichnung, Kennzeichnung KI-generierter Inhalte, "
        "Deepfake-Offenlegung).",
    )
    massnahmen: list[str] = Field(
        default_factory=list,
        description="Konkrete, umsetzbare To-dos zur Herstellung der Konformitaet.",
    )
    fundstellen: list[str] = Field(
        default_factory=list,
        description="Belegstellen aus dem Verordnungstext, auf die sich die "
        "Einordnung stuetzt (z. B. 'Article 5(1)(a)', 'Annex III(4)', "
        "'Recital 48'). NUR Stellen nennen, die per Werkzeug gefunden wurden.",
    )
    offene_fragen: list[str] = Field(
        default_factory=list,
        description="Punkte, die ein Mensch klaeren muss, weil die Beschreibung "
        "sie nicht hergibt. Lieber hier nachfragen als Annahmen treffen.",
    )
    confidence: float = Field(
        ge=0, le=1,
        description="Sicherheit der Einordnung (0..1). Niedrig, wenn die "
        "Beschreibung duenn ist.",
    )
    hinweis: str = Field(
        default="Automatische Ersteinschaetzung, keine Rechtsberatung.",
        description="Fester Disclaimer.",
    )


SYSTEM_PROMPT = (
    "Du bist ein Compliance-Assistent fuer die Ersteinschaetzung von "
    "KI-Anwendungsfaellen nach der Verordnung (EU) 2024/1689 (EU AI Act). "
    "Der Nutzer beschreibt einen geplanten oder bestehenden KI-Einsatz. "
    "Ordne ihn strukturiert ein.\n\n"
    "WICHTIG - Vorgehen: Rate nicht aus dem Gedaechtnis. Nutze VOR der "
    "Einordnung das Werkzeug 'eu_ai_act_suche' mehrfach mit gezielten "
    "englischen Suchbegriffen, um den einschlaegigen Verordnungstext zu holen, "
    "z. B. 'prohibited practices Article 5', 'Annex III high-risk employment "
    "recruitment', 'transparency obligations Article 50 chatbot', "
    "'general-purpose AI model obligations', plus die zum Fall passenden "
    "Stichworte. Stuetze begruendung und fundstellen AUSSCHLIESSLICH auf die "
    "zurueckgegebenen Textstellen und zitiere sie (Artikel/Anhang/Recital).\n\n"
    "Pruefreihenfolge:\n"
    "1. Verbotene Praktiken (Art. 5) -> risikoklasse 'verboten'.\n"
    "2. Hochrisiko (Art. 6 i.V.m. Anhang I oder Anhang III) -> 'hochrisiko'; "
    "nenne die konkrete Anhang-III-Kategorie.\n"
    "3. Transparenzpflichten (Art. 50), z. B. Chatbots, KI-generierte Inhalte, "
    "Deepfakes -> mindestens 'begrenztes_risiko'.\n"
    "4. Sonst 'minimales_risiko'.\n"
    "Pruefe GPAI-Pflichten (Kap. V) separat.\n\n"
    "Regeln: Antworte inhaltlich auf Deutsch. Ueberklassifiziere nicht, aber "
    "verharmlose auch nicht. Wenn die Suche nichts Einschlaegiges liefert oder "
    "die Beschreibung entscheidungsrelevante Angaben nicht hergibt (Datenarten, "
    "betroffene Personen, Automatisierungsgrad, Einsatzkontext), setze "
    "confidence niedrig und stelle die fehlenden Punkte unter offene_fragen. "
    "Triff keine erfundenen Annahmen. Dies ist eine technische "
    "Ersteinschaetzung, keine Rechtsberatung."
)


async def eu_ai_act_suche(suchbegriff: str) -> str:
    """Durchsucht den Volltext der EU-KI-Verordnung (2024/1689) und gibt die
    relevantesten Textstellen zurueck (Erwaegungsgruende, Artikel, Anhaenge).
    Nutze praezise englische Suchbegriffe, da der Verordnungstext Englisch ist.
    Mehrfach mit verschiedenen Begriffen aufrufen, um alle relevanten Stellen
    (verbotene Praktiken, Hochrisiko/Anhang III, Transparenz, Definitionen,
    GPAI) abzudecken."""
    try:
        from aiact_rag import retrieve
        hits = await retrieve(suchbegriff, k=6)
    except Exception as e:  # Index evtl. noch nicht ingestiert
        return f"Suche nicht verfuegbar ({e})."
    if not hits:
        return "Keine passenden Textstellen gefunden."
    return "\n\n".join(f"[{h['section']}] {h['content']}" for h in hits)


_agents: dict[str, Agent] = {}


def get_agent(api_key: str) -> Agent:
    """Agent pro API-Key (der Key stammt aus der eingehenden Anfrage). Gecacht,
    da in der Praxis stets derselbe Key (sk-librechat-admin) verwendet wird."""
    if api_key not in _agents:
        model = OpenAIChatModel(
            AIACT_BACKEND_MODEL,
            provider=OpenAIProvider(
                base_url="http://litellm:4000",
                api_key=api_key,
            ),
        )
        _agents[api_key] = Agent(
            model,
            output_type=AIActEinordnung,
            system_prompt=SYSTEM_PROMPT,
            tools=[eu_ai_act_suche],
        )
    return _agents[api_key]


def _content_to_text(content) -> str:
    """Normalisiert OpenAI-Message-Content (String oder Parts-Liste) zu Text."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return "\n".join(parts).strip()
    return ""


async def einordnen(messages: list[dict]) -> tuple[AIActEinordnung, tuple[int, int]]:
    """Nimmt die komplette OpenAI-Konversation. Alle Turns ausser dem letzten
    User-Turn werden als message_history uebergeben, damit Folgefragen die
    Einordnung praezisieren koennen; der letzte User-Turn ist der Prompt."""
    history: list = []
    last_user = ""
    for msg in messages:
        role = msg.get("role")
        text = _content_to_text(msg.get("content"))
        if not text:
            continue
        if role == "user":
            if last_user:
                history.append(ModelRequest(parts=[UserPromptPart(content=last_user)]))
            last_user = text
        elif role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=text)]))
        # system-Turns bewusst ignoriert: der Agent hat seinen eigenen
        # system_prompt, ein zweiter wuerde die Systematik verwaessern.

    from aiact_rag import current_key
    result = await get_agent(current_key()).run(last_user, message_history=history or None)
    return result.output, _usage_tokens(result)


def _usage_tokens(result) -> tuple[int, int]:
    """Aggregierte Token ueber alle internen Modell-Runden (inkl. Tool-Rounds).
    Feldnamen variieren je pydantic_ai-Version -> defensiv auslesen."""
    try:
        u = result.usage()
    except Exception:
        return 0, 0
    pt = getattr(u, "request_tokens", None) or getattr(u, "input_tokens", None) or 0
    ct = getattr(u, "response_tokens", None) or getattr(u, "output_tokens", None) or 0
    return int(pt), int(ct)


_RISIKO_LABEL = {
    "verboten": "🚫 Verbotene Praktik (Art. 5)",
    "hochrisiko": "🔴 Hochrisiko",
    "begrenztes_risiko": "🟡 Begrenztes Risiko (Transparenzpflichten)",
    "minimales_risiko": "🟢 Minimales Risiko",
}

_ROLLE_LABEL = {
    "anbieter": "Anbieter",
    "betreiber": "Betreiber",
    "einfuehrer": "Einführer",
    "haendler": "Händler",
    "unklar": "unklar",
}


def _liste(items: list[str]) -> str:
    if not items:
        return "_keine_"
    return "\n".join(f"- {i}" for i in items)


def format_markdown(r: AIActEinordnung) -> str:
    """Rendert die strukturierte Einordnung als lesbares Markdown fuer die
    Chat-Oberflaeche."""
    lines = [
        f"## EU-AI-Act-Einordnung: {_RISIKO_LABEL.get(r.risikoklasse, r.risikoklasse)}",
        "",
        f"**Rolle:** {_ROLLE_LABEL.get(r.rolle, r.rolle)}  ·  "
        f"**Konfidenz:** {round(r.confidence * 100)} %  ·  "
        f"**GPAI-Pflichten:** {'ja' if r.gpai_betroffen else 'nein'}",
        "",
        "### Begründung",
        r.begruendung,
    ]
    if r.annex_iii_kategorien:
        lines += ["", "### Einschlägige Anhang-III-Kategorien", _liste(r.annex_iii_kategorien)]
    if r.transparenzpflichten:
        lines += ["", "### Transparenzpflichten (Art. 50)", _liste(r.transparenzpflichten)]
    lines += ["", "### Empfohlene Maßnahmen", _liste(r.massnahmen)]
    if r.offene_fragen:
        lines += ["", "### Offene Fragen (bitte klären)", _liste(r.offene_fragen)]
    if r.fundstellen:
        lines += ["", "### Fundstellen", _liste(r.fundstellen)]
    lines += ["", "---", f"_{r.hinweis}_"]
    return "\n".join(lines)
