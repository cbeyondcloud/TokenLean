"""
RouteLLM HTTP Sidecar — G06 routing service.

POST /route
  Request:  { "messages": [...], "router": "bert", "threshold": 0.4066,
              "strong_model": "gpt-4-1106-preview", "weak_model": "gpt-4o-mini" }
            header X-Embedding-Key: the tenant's own OpenAI key (mf and sw_ranking only)
  Response: { "routed_model": "gpt-4o-mini", "reason": "below_threshold", "router_used": "bert" }
            (+ "embedding_usage": {"model", "prompt_tokens"} when the router embedded)

The answer is always one of the two model names in the request (G06 maps it by exact name).
The sidecar only DECIDES: `Controller.route()` scores the last turn and makes no completion
call, so no conversation is sent to a model. The `mf` and `sw_ranking` routers do embed that
turn with OpenAI, with the key G06 sends for the request, and report the usage so G06 can
price the call.

POST /health
  Response: { "status": "ok" }
"""
import asyncio
import contextvars
import hashlib
import importlib
import logging
import os
import threading
from collections import OrderedDict
from typing import List, Dict, Any, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI(title="RouteLLM Sidecar", version="1.0.0")

# route() answers with one name of the Controller's model pair. These labels only say which
# side won; the response carries the names the caller sent.
_STRONG, _WEAK = "strong", "weak"
_controllers: Dict[str, Any] = {}

# RouteLLM's threshold per router for ~50% strong-model calls (`routellm.calibrate_threshold
# --strong-model-pct 0.5` on its published Chatbot Arena scores); G06 uses the same table. bert
# is the default router: its checkpoint is Apache-2.0, while mf's has no licence.
_THRESHOLDS = {"bert": 0.4066, "mf": 0.11593, "sw_ranking": 0.21647, "causal_llm": 0.0962}
_DEFAULT_ROUTER = "bert"

# ── The tenant's own embedding key (backlog #99) ──────────────────────────────
# mf and sw_ranking embed with ONE module-level OpenAI client that RouteLLM builds at import
# from the environment (routellm/routers/similarity_weighted/utils.py; routers.py and
# matrix_factorization/model.py import that name). The key is each tenant's own, sent by G06
# per request, so that client is replaced in all three modules by one that embeds with the
# request's key. RouteLLM builds its client even for bert and openai refuses to build one with
# no key, so the import gets a stand-in value that nothing uses.
_EMBEDDING_ROUTERS = ("mf", "sw_ranking")
_ROUTELLM_CLIENT_MODULES = ("routellm.routers.similarity_weighted.utils",
                            "routellm.routers.matrix_factorization.model",
                            "routellm.routers.routers")
_request_key: contextvars.ContextVar = contextvars.ContextVar("embedding_key", default=None)
_request_usage: contextvars.ContextVar = contextvars.ContextVar("embedding_usage", default=None)
_CLIENTS: "OrderedDict[str, Any]" = OrderedDict()   # hash of a key -> its client (LRU)
_CLIENTS_MAX = 64
_CLIENTS_LOCK = threading.Lock()
_client_installed = False


def _openai_client(key: str):
    from openai import OpenAI
    return OpenAI(api_key=key)


def _client_for(key: str):
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    with _CLIENTS_LOCK:
        client = _CLIENTS.pop(digest, None) or _openai_client(key)
        _CLIENTS[digest] = client
        while len(_CLIENTS) > _CLIENTS_MAX:
            _CLIENTS.popitem(last=False)
    return client


class _Embeddings:
    def create(self, **kwargs):
        key = _request_key.get()
        if not key:
            raise RuntimeError("no embedding key for this request")
        response = _client_for(key).embeddings.create(**kwargs)
        usage = _request_usage.get()
        if usage is not None:
            usage["model"] = kwargs.get("model") or usage.get("model")
            used = getattr(getattr(response, "usage", None), "prompt_tokens", 0) or 0
            usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + int(used)
        return response


class _RequestKeyClient:
    """Stands in for RouteLLM's module-level OpenAI client: embeds with the request's key."""
    embeddings = _Embeddings()


def _install_request_key_client() -> None:
    global _client_installed
    if _client_installed:
        return
    client = _RequestKeyClient()
    for name in _ROUTELLM_CLIENT_MODULES:
        module = importlib.import_module(name)
        if not hasattr(module, "OPENAI_CLIENT"):
            raise RuntimeError(f"{name} has no OPENAI_CLIENT: RouteLLM changed, so the "
                               "tenant's key cannot be used for its embedding")
        module.OPENAI_CLIENT = client
    _client_installed = True


def _get_controller(router: str):
    """The RouteLLM Controller for one router, built on first use. Building a router loads
    its checkpoint (causal_llm's is an 8B model), so only routers actually asked for load."""
    if router not in _controllers:
        os.environ.setdefault("OPENAI_API_KEY", "unused-the-key-comes-with-each-request")
        from routellm.controller import Controller

        if router in _EMBEDDING_ROUTERS:
            _install_request_key_client()
        _controllers[router] = Controller(routers=[router], strong_model=_STRONG,
                                          weak_model=_WEAK)
        logger.info("RouteLLM Controller loaded for router %r", router)
    return _controllers[router]


def _last_turn_text(messages: List[Dict[str, Any]]) -> str:
    """What RouteLLM routes on: the last turn's text (text parts of a list content)."""
    content = messages[-1].get("content", "")
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content
                           if isinstance(part, dict) and part.get("type") == "text")
    return content if isinstance(content, str) else str(content)


class RouteRequest(BaseModel):
    messages: List[Dict[str, Any]]
    router: str = _DEFAULT_ROUTER
    threshold: Optional[float] = None   # None: the router's own calibration (_THRESHOLDS)
    strong_model: Optional[str] = None
    weak_model: Optional[str] = None


class RouteResponse(BaseModel):
    routed_model: str
    reason: str
    router_used: str
    embedding_usage: Optional[Dict[str, Any]] = None    # mf / sw_ranking only


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/route", response_model=RouteResponse, response_model_exclude_none=True)
async def route(req: RouteRequest,
                embedding_key: Optional[str] = Header(None, alias="X-Embedding-Key")):
    """Route a request to the strong or the weak model using RouteLLM."""
    if not req.messages:
        raise HTTPException(status_code=400, detail="Messages cannot be empty")
    embeds = req.router in _EMBEDDING_ROUTERS
    if embeds and not embedding_key:
        raise HTTPException(status_code=400, detail=(
            f"the {req.router} router embeds with the tenant's own OpenAI key: "
            "send it in X-Embedding-Key"))

    strong_model = req.strong_model or os.getenv("ROUTELLM_STRONG_MODEL", "gpt-4-1106-preview")
    weak_model = req.weak_model or os.getenv("ROUTELLM_WEAK_MODEL", "gpt-4o-mini")
    threshold = (req.threshold if req.threshold is not None
                 else _THRESHOLDS.get(req.router, _THRESHOLDS[_DEFAULT_ROUTER]))
    usage: Optional[Dict[str, Any]] = {} if embeds else None
    # This request's own context, which to_thread copies into the worker thread: concurrent
    # requests each embed with their own key.
    _request_key.set(embedding_key if embeds else None)
    _request_usage.set(usage)
    try:
        controller = _get_controller(req.router)
        # Synchronous (it may embed the prompt) — off the event loop.
        side = await asyncio.to_thread(
            controller.route, _last_turn_text(req.messages), req.router, threshold)
    except Exception as exc:  # noqa: BLE001 — any failure answers 500; its cause may quote the key
        message = str(exc).replace(embedding_key, "<key>") if embedding_key else str(exc)
        logger.error("Routing failed: %s", message)
        raise HTTPException(status_code=500, detail=f"Routing error: {message}") from None

    # RouteLLM exposes no confidence, so none is reported: the threshold (or one minus it) was
    # sent under that name once, and it looked like a score without being one.
    if side == _WEAK:
        routed_model, reason = weak_model, "below_threshold"
    elif side == _STRONG:
        routed_model, reason = strong_model, "above_threshold"
    else:
        raise HTTPException(status_code=500, detail=f"Router returned an unknown model {side!r}")
    return RouteResponse(
        routed_model=routed_model,
        reason=reason,
        router_used=req.router,
        embedding_usage=usage or None,
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
