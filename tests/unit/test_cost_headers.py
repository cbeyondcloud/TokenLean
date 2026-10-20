"""x-tokenlean-cost-usd: the ACTUAL cost of the call on every non-streamed response.

The savings headers said what was saved, never what was paid; the actual cost was in the
body (`_token_opt.cost_actual_usd`, OpenAI shape only), so a caller on another protocol, or
one that does not parse the body, had no per-call cost at all.
"""
from datetime import datetime, timezone

import pytest

import main
from savings.models import SavingsRecord


class _Ctx:
    def __init__(self, cost_actual_usd: float):
        self.params = {}
        self.savings = SavingsRecord(
            request_id="req-cost", user_id="u", timestamp=datetime.now(timezone.utc),
            model_requested="gpt-4o-mini", routed_model="gpt-4o-mini", baseline_tokens=10,
        )
        self.savings.cost_actual_usd = cost_actual_usd


def test_savings_headers_carry_actual_cost():
    headers = main._savings_headers(_Ctx(0.0012345), 0.0)
    assert headers["x-tokenlean-cost-usd"] == f"{0.0012345:.6f}"


def test_savings_headers_cost_zero_when_no_provider_call():
    """A cache hit or a bypass paid nothing: the header says so rather than going missing."""
    headers = main._savings_headers(_Ctx(0.0), 0.0)
    assert headers["x-tokenlean-cost-usd"] == "0.000000"


@pytest.mark.asyncio
async def test_chat_completion_response_has_cost_header(client):
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini",
              "messages": [{"role": "user", "content": "What is the capital of France?"}]},
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 200
    header = response.headers["x-tokenlean-cost-usd"]
    assert float(header) >= 0
    body_cost = response.json()["_token_opt"]["cost_actual_usd"]
    assert header == f"{body_cost:.6f}"
