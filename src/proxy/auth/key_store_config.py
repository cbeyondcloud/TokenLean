"""Which store proxy keys are validated against, chosen by config (ST-9).

    key_store:
      backend: blob            # blob (default) | postgres
      refresh_seconds: 5       # postgres: each replica re-reads every key this often (min 5)
      max_stale_seconds: 300   # postgres: refuse every key (503) after this long unrefreshed
                               # (0 = never; else at least twice refresh_seconds)

``blob`` is the local JSON file / Secret Manager store, exactly as before: an absent section
means it. ``postgres`` validates against the ``proxy_keys`` table (auth/pg_key_store.py),
which lifts the blob's size limit and serialises writers across replicas. Then:

* until the database is wired, no key validates (:func:`install_pending`): the blob is not
  consulted, since a key revoked in Postgres may still be in it;
* once it is (:func:`wire_postgres`, called by main's database wiring), each replica
  re-reads every key each ``refresh_seconds``. A key revoked through any replica is refused
  by that replica at once and by every other within ``refresh_seconds``;
* a replica that has not managed to re-read for ``max_stale_seconds`` refuses every key
  (503) until it does, so a revocation is never honoured later than that, even while the
  database is unreachable. 0 keeps serving the last snapshot instead.

A setting that cannot be read is refused (ValueError), never guessed: a typo in ``backend``
must not validate keys against the wrong store. The backend is chosen at startup; changing
it takes a restart. A host that installs its own backend (the commercial app, with
PROXY_KEYS_BACKEND=postgres) leaves this section at ``blob``.
"""
import logging
from typing import Any, Dict

from auth import api_key_manager as km

logger = logging.getLogger(__name__)

BACKENDS = ("blob", "postgres")
DEFAULTS = {"backend": "blob", "refresh_seconds": 5, "max_stale_seconds": 300}
MIN_REFRESH_SECONDS = 5  # CacheRefresher's own floor


def _number(section: Dict[str, Any], key: str) -> float:
    value = section.get(key, DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"key_store.{key} must be a number of seconds, 0 or more")
    return value


def key_store_settings(config: Any) -> Dict[str, Any]:
    """The ``key_store`` section with its defaults, checked. Raises ValueError."""
    section = config.get("key_store") if isinstance(config, dict) else None
    if section is None:
        return dict(DEFAULTS)
    if not isinstance(section, dict):
        raise ValueError("key_store must be a mapping")
    backend = section.get("backend", DEFAULTS["backend"])
    if not isinstance(backend, str) or backend.strip().lower() not in BACKENDS:
        raise ValueError(f"key_store.backend must be one of {', '.join(BACKENDS)}")
    refresh = max(MIN_REFRESH_SECONDS, int(_number(section, "refresh_seconds")))
    max_stale = int(_number(section, "max_stale_seconds"))
    if max_stale and max_stale < 2 * refresh:
        raise ValueError("key_store.max_stale_seconds must be 0 or at least twice "
                         "refresh_seconds (one failed refresh must not refuse every key)")
    return {"backend": backend.strip().lower(), "refresh_seconds": refresh,
            "max_stale_seconds": max_stale}


def _not_wired_yet():
    return None  # "no fresh data": the empty cache stays, so no key validates


def _refuse_write(_store):
    raise RuntimeError("the Postgres key store is not wired yet")


def install_pending(settings: Dict[str, Any]) -> None:
    """For ``postgres``: install a stand-in backend under which no key validates and no key
    is written, until :func:`wire_postgres` replaces it. Nothing for ``blob``."""
    if settings.get("backend") != "postgres":
        return
    km.install_key_store_backend(_not_wired_yet, _refuse_write, name="postgres (not wired)",
                                 max_stale_seconds=settings.get("max_stale_seconds", 0))
    km.clear_cache()
    logger.info("key store: postgres; no proxy key validates until the database is wired")


async def wire_postgres(pool, loop, settings: Dict[str, Any]):
    """Create the tables if this role may, copy the blob's keys in once (only into an empty
    table, never a revoked hash), install the Postgres backend with the current keys loaded,
    and return the :class:`pg_key_store.CacheRefresher` the caller must run."""
    from auth import pg_key_store

    await pg_key_store.ensure_proxy_keys_schema(pool)
    await pg_key_store.import_blob_store_once(pool)
    keys = await pg_key_store.load_all(pool)
    load_fn, persist_fn = pg_key_store.make_backend(pool, loop)
    km.install_key_store_backend(
        load_fn, persist_fn, name="postgres",
        transact_fn=pg_key_store.make_transact(pool, loop),
        max_stale_seconds=settings.get("max_stale_seconds", 0))
    km.replace_cache(keys)
    logger.info("key store: postgres wired (%d key(s)); refresh every %ss, refuse after %ss "
                "unrefreshed", len(keys), settings["refresh_seconds"],
                settings.get("max_stale_seconds", 0) or "never")
    return pg_key_store.CacheRefresher(pool, settings["refresh_seconds"])
