import os
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider


class TicketClassification(BaseModel):
    category: str
    priority: str
    summary: str
    requires_human: bool


_classifier = None

def get_classifier() -> Agent:
    global _classifier
    if _classifier is None:
        model = OpenAIChatModel(
            "gpt-4.1-mini",
            provider=OpenAIProvider(
                base_url="http://litellm:4000",
                api_key=os.getenv("LITELLM_MASTER_KEY", "sk-dummy"),
            ),
        )
        _classifier = Agent(
            model,
            output_type=TicketClassification,
            system_prompt=(
                "You are a support ticket classifier for a small business software company. "
                "Given an incoming customer message, return: "
                "category (billing/technical/general), "
                "priority (low/medium/high), "
                "summary (one sentence), "
                "and requires_human (true if needs human review)."
            ),
        )
    return _classifier


async def classify_ticket(message: str) -> TicketClassification:
    result = await get_classifier().run(message)
    return result.output
