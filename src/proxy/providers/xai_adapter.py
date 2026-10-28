"""xAI (Grok) adapter — OpenAI-compatible via LiteLLM (``xai/`` namespace)."""
from providers import register_adapter
from providers.generic_adapter import GenericLiteLLMAdapter


@register_adapter("xai")
class XAIAdapter(GenericLiteLLMAdapter):
    #: OpenTelemetry GenAI well-known provider name (gen_ai.provider.name).
    SEMCONV_PROVIDER_NAME = "x_ai"
    PROVIDER_NAME = "xai"
    LITELLM_PREFIX = "xai"
