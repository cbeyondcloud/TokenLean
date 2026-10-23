"""Groq adapter — OpenAI-compatible via LiteLLM (``groq/`` namespace), fast inference."""
from providers import register_adapter
from providers.generic_adapter import GenericLiteLLMAdapter


@register_adapter("groq")
class GroqAdapter(GenericLiteLLMAdapter):
    #: OpenTelemetry GenAI well-known provider name (gen_ai.provider.name).
    SEMCONV_PROVIDER_NAME = "groq"
    PROVIDER_NAME = "groq"
    LITELLM_PREFIX = "groq"
