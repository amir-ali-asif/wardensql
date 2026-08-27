from .fake import FakeProvider

__all__ = ["FakeProvider", "get_provider"]


def get_provider(settings):
    """Factory: build the configured LLM provider."""
    if settings.llm_provider == "fake":
        return FakeProvider()
    # groq and any OpenAI-compatible endpoint share the same client.
    from .groq_provider import GroqProvider
    return GroqProvider(settings)
