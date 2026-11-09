"""Keep Alexa's normal conversational chat on its original Ollama provider.

OpenAI is intentionally not selected for normal chat. This preserves the previous
Ollama-backed behavior and avoids making chat depend on OpenAI quota or rate limits.
"""
from bot.services.ollama_ai import ollama_chat


async def ai_chat_completion(messages, *, system="", temperature=0.7, max_tokens=500, json_mode=False):
    """Generate normal Alexa chat using the existing Ollama configuration."""
    return await ollama_chat(
        messages,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
    )
