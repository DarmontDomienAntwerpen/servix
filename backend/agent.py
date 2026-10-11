"""
agent.py — Agentic troubleshooting loop for Servix.
Receives technician messages, selects tools, searches manuals, and generates responses.
"""

import os
import json
import logging

from openai import AsyncOpenAI
from duckduckgo_search import DDGS

from rag import search_manual
from guardrails import validate_response, is_conversational
from memory import get_conversation_history, save_message
from language import detect_language
from prompts import AGENT_SYSTEM_PROMPT, LANGUAGE_REMINDER_PROMPTS

logger = logging.getLogger(__name__)
openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

MAX_TOOL_ITERATIONS = 5


def language_reminder(user_message: str) -> dict:
    """Last system message on every call. Without it, the Dutch system prompt and
    Dutch manual excerpts pull answers to Dutch even when the technician writes English."""
    key = detect_language(user_message)
    return {"role": "system", "content": LANGUAGE_REMINDER_PROMPTS[key]}


# ── Tool definitions ──────────────────────────────────────────────────────────
# Descriptions are in Dutch, matching the system prompt in prompts.py.

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_manual",
            "description": (
                "Zoek in technische handleidingen naar informatie over een storing, "
                "foutcode, onderdeel of procedure. Gebruik dit altijd vóór je een "
                "diagnose of stap-voor-stap uitleg geeft."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "De zoekvraag, zo specifiek mogelijk",
                    },
                    "brand": {
                        "type": "string",
                        "description": "Merknaam van het toestel (bv. Vaillant, Daikin)",
                    },
                    "error_code": {
                        "type": "string",
                        "description": "Foutcode als die bekend is (bv. F.28, U4)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_followup_question",
            "description": (
                "Stel een gerichte diagnosevraag aan de monteur als je meer informatie "
                "nodig hebt om het probleem te identificeren."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "De vraag die je aan de monteur wil stellen",
                    }
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Zoek op officiële fabrikantssites als de handleidingen geen antwoord bevatten. "
                "Gebruik dit ALLEEN nadat de monteur expliciet toestemming heeft gegeven. "
                "Meld altijd dat de info van het internet komt en mogelijk niet up-to-date is."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Zoekopdracht, inclusief merknaam voor betere resultaten",
                    },
                    "brand": {
                        "type": "string",
                        "description": "Merknaam om de zoekopdracht te beperken tot officiële site",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


# ── Tool execution ────────────────────────────────────────────────────────────

async def execute_tool(
    tool_name: str,
    tool_args: dict,
    context: dict,
) -> str:
    """
    Execute a tool call and return the result as a string.

    Args:
        tool_name: Name of the tool to execute
        tool_args: Arguments from the model's tool call
        context: Runtime context (user_id, company_id, session_id)

    Returns:
        Tool result as a string for the model to read
    """
    if tool_name == "search_manual":
        results = await search_manual(
            query=tool_args["query"],
            brand=tool_args.get("brand"),
            error_code=tool_args.get("error_code"),
        )
        if not results:
            return "Geen relevante informatie gevonden in de handleidingen."
        chunks = []
        for r in results:
            source = f"{r['source_file']} p.{r['page']}" if r["page"] else r["source_file"]
            chunks.append(f"[Bron: {source}]\n{r['text']}")
        return "\n\n---\n\n".join(chunks)

    elif tool_name == "search_web":
        return await _web_search(
            query=tool_args["query"],
            brand=tool_args.get("brand"),
        )

    elif tool_name == "ask_followup_question":
        return tool_args["question"]

    return f"Onbekende tool: {tool_name}"


BRAND_SITES = {
    "vaillant": "site:vaillant.be OR site:vaillant.com",
    "daikin": "site:daikin.eu OR site:daikin.be",
    "intergas": "site:intergas.nl",
    "remeha": "site:remeha.com OR site:remeha.be",
    "bosch": "site:bosch-thermotechnology.com",
    "viessmann": "site:viessmann.com OR site:viessmann.be",
    "baxi": "site:baxi.be OR site:baxi.eu",
    "bulex": "site:bulex.be",
    "ferroli": "site:ferroli.com",
}


async def _web_search(query: str, brand: str | None = None) -> str:
    """
    Search official manufacturer websites via DuckDuckGo.

    Restricts results to known manufacturer domains to avoid unreliable sources.
    Returns top 3 results as formatted text.

    Args:
        query: Search query
        brand: Optional brand to restrict the search domain

    Returns:
        Formatted search results with titles, URLs, and snippets
    """
    site_filter = ""
    if brand:
        site_filter = BRAND_SITES.get(brand.lower(), "")

    full_query = f"{query} {site_filter}".strip()

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(full_query, max_results=3))

        if not results:
            return "Geen relevante resultaten gevonden op officiële fabrikantssites."

        lines = []
        for r in results:
            lines.append(f"🌐 {r.get('title', '')}")
            lines.append(f"   {r.get('href', '')}")
            lines.append(f"   {r.get('body', '')}\n")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Web search failed for query='{query}': {e}")
        return "Webzoekactie mislukt. Probeer later opnieuw."


# ── Agent loop ────────────────────────────────────────────────────────────────

async def run_agent(
    user_message: str,
    context: dict,
) -> str:
    """
    Run the agentic loop for a single incoming technician message.

    Builds the message history, calls OpenAI with tool definitions,
    executes any tool calls, and returns the final text response.

    Args:
        user_message: The technician's incoming message
        context: Dict with user_id, company_id, session_id

    Returns:
        The assistant's final response text
    """
    session_id = context["session_id"]

    # Build conversation history
    history = await get_conversation_history(session_id)

    messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # Save the user message
    await save_message(
        user_id=context["user_id"],
        company_id=context["company_id"],
        session_id=session_id,
        role="user",
        message=user_message,
    )

    # Agentic tool loop
    used_web_search = False

    for iteration in range(MAX_TOOL_ITERATIONS):
        response = await openai.chat.completions.create(
            model="gpt-4o",
            messages=messages + [language_reminder(user_message)],
            tools=TOOLS,
            tool_choice="auto",
        )

        choice = response.choices[0]
        messages.append(choice.message)

        # No tool call → final answer, run through guardrails
        if choice.finish_reason == "stop":
            final_text = choice.message.content or ""
            conversational = is_conversational(final_text)
            validated = validate_response(
                final_text,
                from_web=used_web_search,
                requires_source=not conversational,
            )
            await save_message(
                user_id=context["user_id"],
                company_id=context["company_id"],
                session_id=session_id,
                role="assistant",
                message=validated,
            )
            return validated

        # Execute tool calls
        if choice.message.tool_calls:
            for tool_call in choice.message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)

                logger.info(f"Tool call: {tool_name}({tool_args})")

                if tool_name == "search_web":
                    used_web_search = True

                result = await execute_tool(tool_name, tool_args, context)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

    # Fallback if max iterations reached
    fallback = "Ik kon geen volledig antwoord genereren. Probeer de vraag anders te formuleren."
    await save_message(
        user_id=context["user_id"],
        company_id=context["company_id"],
        session_id=session_id,
        role="assistant",
        message=fallback,
    )
    return fallback
