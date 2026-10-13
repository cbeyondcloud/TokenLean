"""Unit tests for the RouteLLM sidecar (src/routellm-sidecar/app.py).

The sidecar must DECIDE, never complete: RouteLLM's `chat.completions.create` routes and then
calls the chosen model, a billed completion carrying the tenant's conversation. And it must
answer with the model names G06 sent, since G06 maps the answer by exact name. RouteLLM itself
is replaced by a fake that fails the test if a completion is requested.
"""
import importlib.util
import os
import sys
import types

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("pydantic")
pytest.importorskip("uvicorn")
from fastapi.testclient import TestClient  # noqa: E402

_SIDECAR_APP = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "routellm-sidecar", "app.py"))


# RouteLLM's mf and sw_ranking embed with ONE module-level OpenAI client that these three
# modules hold (similarity_weighted.utils builds it; the other two import the name).
_CLIENT_MODULES = ("routellm.routers.similarity_weighted.utils",
                   "routellm.routers.matrix_factorization.model", "routellm.routers.routers")
KEY = "sk-tenant-key-0001"


class _FakeController:
    """routellm.controller.Controller: `route()` returns one name of its model pair, and an
    embedding router embeds the prompt through the module-level client, as RouteLLM does."""
    built = []

    def __init__(self, routers, strong_model, weak_model, **kwargs):
        self.routers, self.strong, self.weak = list(routers), strong_model, weak_model
        self.env_key_at_build = os.environ.get("OPENAI_API_KEY")   # what RouteLLM's import saw
        self.routed = []
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._completion))
        _FakeController.built.append(self)

    def _completion(self, **kwargs):
        raise AssertionError("the sidecar made a completion call")

    def route(self, prompt, router, threshold):
        self.routed.append((prompt, router, threshold))
        if router in ("mf", "sw_ranking"):
            sys.modules["routellm.routers.routers"].OPENAI_CLIENT.embeddings.create(
                input=[prompt], model="text-embedding-3-small")
        return self.strong if "prove" in prompt else self.weak


class _OpenAI:
    """openai.OpenAI as the sidecar builds it per key; records what each key embedded."""
    made, embedded = [], []

    def __init__(self, api_key):
        self.api_key = api_key
        _OpenAI.made.append(api_key)
        self.embeddings = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        import time
        time.sleep(0.02)                        # lets two requests overlap
        _OpenAI.embedded.append((self.api_key, kwargs["input"][0]))
        return types.SimpleNamespace(usage=types.SimpleNamespace(prompt_tokens=7))


def _load_app(monkeypatch):
    _FakeController.built, _OpenAI.made, _OpenAI.embedded = [], [], []
    monkeypatch.setenv("OPENAI_API_KEY", "set-by-the-test")   # restored after the test
    controller = types.ModuleType("routellm.controller")
    controller.Controller = _FakeController
    modules = {"routellm": types.ModuleType("routellm"), "routellm.controller": controller}
    for name in _CLIENT_MODULES:
        parts = name.split(".")
        for i in range(1, len(parts)):
            modules.setdefault(".".join(parts[:i]), types.ModuleType(".".join(parts[:i])))
        modules[name] = types.ModuleType(name)
        modules[name].OPENAI_CLIENT = "the env-built client"
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("routellm_sidecar_app", _SIDECAR_APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_openai_client", _OpenAI)
    return module


@pytest.fixture
def app_module(monkeypatch):
    return _load_app(monkeypatch)


@pytest.fixture
def client(app_module):
    return TestClient(app_module.app)


def _route(client, content, router="mf", key=KEY, **extra):
    body = {"messages": [{"role": "system", "content": "be brief"},
                         {"role": "user", "content": content}],
            "router": router, "threshold": 0.2,
            "strong_model": "tenant-strong", "weak_model": "tenant-weak", **extra}
    return client.post("/route", json=body, headers={"X-Embedding-Key": key} if key else {})


class TestRoute:
    def test_a_weak_decision_answers_with_the_weak_name_g06_sent(self, client):
        r = _route(client, "hi there")
        assert r.status_code == 200, r.text
        assert r.json()["routed_model"] == "tenant-weak"
        assert r.json()["reason"] == "below_threshold"

    def test_a_strong_decision_answers_with_the_strong_name_g06_sent(self, client):
        r = _route(client, "prove the theorem")
        assert r.status_code == 200, r.text
        assert r.json()["routed_model"] == "tenant-strong"
        assert r.json()["reason"] == "above_threshold"

    @pytest.mark.parametrize("text", ["hi there", "prove the theorem"])
    def test_the_answer_carries_no_made_up_confidence(self, client, text):
        """RouteLLM exposes no confidence; the sidecar used to send the threshold (or one minus
        it) under that name, which looked like a score and was not one."""
        r = _route(client, text, router="bert")
        assert r.status_code == 200, r.text
        assert set(r.json()) == {"routed_model", "reason", "router_used"}

    def test_the_last_turn_and_the_threshold_are_what_is_routed(self, client):
        _route(client, [{"type": "text", "text": "prove it"}, {"type": "image_url"}])
        assert _FakeController.built[0].routed == [("prove it", "mf", 0.2)]

    def test_env_names_when_the_request_names_none(self, client, monkeypatch):
        monkeypatch.setenv("ROUTELLM_WEAK_MODEL", "env-weak")
        r = client.post("/route", json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert r.json()["routed_model"] == "env-weak"

    def test_empty_messages_are_refused(self, client):
        assert client.post("/route", json={"messages": []}).status_code == 400


class TestDefaults:
    # bert is the default router (Apache-2.0 checkpoint); each router's default threshold is
    # RouteLLM's calibration for ~50% strong-model calls, the same table G06 uses.
    def test_a_request_naming_no_router_uses_bert_at_its_threshold(self, client):
        r = client.post("/route", json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert r.json()["router_used"] == "bert"
        assert _FakeController.built[0].routed == [("hi", "bert", 0.4066)]

    def test_a_named_router_without_a_threshold_gets_its_own_calibration(self, client):
        client.post("/route", json={"messages": [{"role": "user", "content": "hi"}],
                                    "router": "sw_ranking"}, headers={"X-Embedding-Key": KEY})
        assert _FakeController.built[0].routed == [("hi", "sw_ranking", 0.21647)]


class TestTheTenantsKey:
    """#99: mf and sw_ranking embedded with ONE platform key from the sidecar's environment;
    the key is each tenant's own (the owner's rule), sent per request in X-Embedding-Key."""

    @pytest.mark.parametrize("router", ["mf", "sw_ranking"])
    def test_an_embedding_router_embeds_with_the_request_key(self, client, router):
        r = _route(client, "hi there", router=router)
        assert r.status_code == 200, r.text
        assert _OpenAI.embedded == [(KEY, "hi there")]
        # #108: the usage comes back, so G06 can price the call.
        assert r.json()["embedding_usage"] == {"model": "text-embedding-3-small", "prompt_tokens": 7}

    @pytest.mark.parametrize("router", ["mf", "sw_ranking"])
    def test_an_embedding_router_without_a_key_is_refused(self, client, router):
        r = _route(client, "hi there", router=router, key=None)
        assert r.status_code == 400 and "X-Embedding-Key" in r.json()["detail"]
        assert _OpenAI.embedded == [] and _FakeController.built == []

    def test_bert_needs_no_key_and_embeds_nothing(self, client):
        r = _route(client, "hi there", router="bert", key=None)
        assert r.status_code == 200, r.text
        assert _OpenAI.embedded == [] and "embedding_usage" not in r.json()

    def test_routellm_is_imported_without_a_platform_key(self, monkeypatch):
        """RouteLLM builds its client at import even for bert, and openai refuses no key, so
        the sidecar gives the import a stand-in value before building a router."""
        import os as _os
        client = TestClient(_load_app(monkeypatch).app)
        monkeypatch.delenv("OPENAI_API_KEY")                   # restored after the test
        assert _route(client, "hi", router="bert", key=None).status_code == 200
        assert _FakeController.built[0].env_key_at_build
        assert _os.environ.get("OPENAI_API_KEY")

    def test_one_client_per_key_and_the_env_client_is_never_used(self, client):
        _route(client, "one")
        _route(client, "two")
        _route(client, "three", key="sk-other-tenant")
        assert _OpenAI.made == [KEY, "sk-other-tenant"]
        for name in _CLIENT_MODULES:
            assert sys.modules[name].OPENAI_CLIENT != "the env-built client", name

    def test_two_tenants_at_once_each_embed_with_their_own_key(self, app_module):
        import asyncio

        import httpx

        async def both():
            transport = httpx.ASGITransport(app=app_module.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://sidecar") as c:
                async def one(text, key):
                    return await c.post("/route", headers={"X-Embedding-Key": key}, json={
                        "messages": [{"role": "user", "content": text}], "router": "mf",
                        "strong_model": "s", "weak_model": "w"})
                return await asyncio.gather(*(one(f"text-{i}", f"sk-key-{i}") for i in range(6)))

        answers = asyncio.run(both())
        assert all(a.status_code == 200 for a in answers)
        assert sorted(_OpenAI.embedded) == sorted((f"sk-key-{i}", f"text-{i}") for i in range(6))

    def test_a_routellm_without_the_client_fails_loudly_and_bert_still_works(self, app_module):
        """A RouteLLM upgrade that renames the client must not silently embed some other way."""
        del sys.modules["routellm.routers.routers"].OPENAI_CLIENT
        client = TestClient(app_module.app)
        r = _route(client, "hi there")
        assert r.status_code == 500 and "RouteLLM changed" in r.json()["detail"]
        assert _route(client, "hi there", router="bert", key=None).status_code == 200

    def test_the_key_is_never_logged_or_echoed(self, client, monkeypatch, caplog):
        def leaky(**kwargs):
            raise RuntimeError(f"Incorrect API key provided: {KEY}")

        monkeypatch.setattr(_OpenAI, "_create", lambda self, **kw: leaky(**kw))
        r = _route(client, "hi there")
        assert r.status_code == 500 and KEY not in r.text
        assert all(KEY not in rec.getMessage() for rec in caplog.records)

    def test_the_installed_routellm_still_has_the_client_it_replaces(self):
        """Runs where RouteLLM is installed (the sidecar image): the three names it patches."""
        import importlib
        pytest.importorskip("routellm")
        for name in _CLIENT_MODULES:
            if name in sys.modules and getattr(sys.modules[name], "__file__", None) is None:
                pytest.skip("a stub is loaded")
            assert hasattr(importlib.import_module(name), "OPENAI_CLIENT"), name


class TestControllers:
    def test_only_the_requested_router_is_built_once(self, client):
        # Building a router loads its checkpoint; causal_llm's is an 8B model.
        _route(client, "hi")
        _route(client, "hello")
        _route(client, "hi", router="sw_ranking")
        assert [c.routers for c in _FakeController.built] == [["mf"], ["sw_ranking"]]
