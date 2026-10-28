"""Mistral adapter — OpenAI-compatible via LiteLLM (``mistral/`` namespace)."""
from providers import register_adapter
from providers.generic_adapter import GenericLiteLLMAdapter


@register_adapter("mistral")
class MistralAdapter(GenericLiteLLMAdapter):
    #: OpenTelemetry GenAI well-known provider name (gen_ai.provider.name).
    SEMCONV_PROVIDER_NAME = "mistral_ai"
    PROVIDER_NAME = "mistral"
    LITELLM_PREFIX = "mistral"
