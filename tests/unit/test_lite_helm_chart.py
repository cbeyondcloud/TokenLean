"""deploy/lite/cloud/helm/tokenlean-lite: the LITE Helm chart holds its contract (ST-8 cloud).

The chart runs the LITE proxy on any Kubernetes, with an external (managed) Postgres and
Redis, or a bundled Redis for a dev cluster. What a broken chart costs is a cluster that runs
another config than the local stack proves, a secret written into a values file, or a pod
that the cluster's policy refuses. The file checks run anywhere; the rendering checks run
helm and kubeconform in pinned containers and skip, saying why, without a container engine.
"""
import functools
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_LITE = os.path.join(_REPO, "deploy", "lite")
_CLOUD = os.path.join(_LITE, "cloud")
_CHART = os.path.join(_CLOUD, "helm", "tokenlean-lite")
_VALUES_DIR = os.path.join(_CLOUD, "values")
_ENVS = ("dev", "staging", "prod", "managed")

# Native tools run only in containers, pinned by digest (released on or before 2026-10-15).
HELM_IMAGE = ("alpine/helm:4.3.0@sha256:"
              "a6cf54599ccb99d90cf0712b30f03fdb3cab062e6b94e0418cc4db7e8a1464b2")
HELM3_IMAGE = ("alpine/helm:3.22.0@sha256:"
               "70ed3f7b02ab2eee12aa520ad6870305db8a82516b07aff62f5656c657a26e2a")
KUBECONFORM_IMAGE = ("ghcr.io/yannh/kubeconform:v0.8.0@sha256:"
                     "faffaf43f95aa6425306e1ab8d6fcad72acb9049158f38e574c085ea1ec0f64e")
# The oldest Kubernetes minor still supported upstream when the chart was written.
KUBE_VERSION = "1.32.0"
# Every render sets an image: the values files ship a placeholder tag on purpose.
_IMAGE_SET = ("--set", "image.repository=registry.example.internal/tokenlean-proxy",
              "--set", "image.tag=1.0.0")

# The chart's byte copies (helm reads nothing outside the chart): file -> its source.
COPIES = {
    "files/agentic.yaml": os.path.join(_LITE, "config", "agentic.yaml"),
    "files/langfuse.yaml": os.path.join(_LITE, "config", "langfuse.yaml"),
    "files/config.yaml.template": os.path.join(_REPO, "config", "config.yaml.template"),
    "files/mock_llm.py": os.path.join(_LITE, "local", "mock", "mock_llm.py"),
}


def _read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as fh:
        return fh.read()


def _values(name):
    return yaml.safe_load(_read(os.path.join(_VALUES_DIR, f"{name}.yaml"))) or {}


def _chart_values():
    return yaml.safe_load(_read(os.path.join(_CHART, "values.yaml")))


def _leaves(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _leaves(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _leaves(v, f"{path}[{i}]")
    else:
        yield path, node


def _key_paths(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else str(k)
            yield here
            yield from _key_paths(v, here)


# ── Files (no container engine needed) ──────────────────────────────────────────────────

@pytest.mark.parametrize("copy", sorted(COPIES))
def test_chart_files_are_byte_copies_of_their_source(copy):
    """One source of truth: the chart carries copies only because helm reads nothing outside
    the chart. `python scripts/sync_lite_chart.py` refreshes them."""
    assert _read(os.path.join(_CHART, copy), "rb") == _read(COPIES[copy], "rb"), (
        f"{copy} differs from its source: run python scripts/sync_lite_chart.py")


def _eol_attributes(path):
    out = subprocess.run(["git", "check-attr", "text", "eol", "--",
                          os.path.relpath(path, _REPO).replace(os.sep, "/")],
                         cwd=_REPO, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return [line.rsplit(": ", 1)[-1] for line in out.stdout.splitlines()]


@pytest.mark.parametrize("copy", sorted(COPIES))
def test_each_source_checks_out_with_its_copys_line_endings(copy):
    """A copy pinned to LF next to a source left to core.autocrlf would differ on a Windows
    checkout (autocrlf=true writes the source as CRLF), and the byte checks would fail there."""
    if not shutil.which("git") or not os.path.exists(os.path.join(_REPO, ".git")):
        pytest.skip("not a git checkout")
    assert _eol_attributes(COPIES[copy]) == _eol_attributes(os.path.join(_CHART, copy)), copy


def test_the_sync_script_never_copies_crlf_into_a_copy(tmp_path, monkeypatch, capsys):
    """A clone made before the source was pinned to LF can still hold it as CRLF: the sync
    says how to check it out again, and leaves the LF copy as it is."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sync_lite_chart", os.path.join(_REPO, "scripts", "sync_lite_chart.py"))
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    (tmp_path / "config").mkdir()
    (tmp_path / "chart" / "files").mkdir(parents=True)
    (tmp_path / "config" / "c.template").write_bytes(b"a: 1\r\nb: 2\r\n")
    (tmp_path / "chart" / "files" / "c.template").write_bytes(b"a: 1\nb: 2\n")
    monkeypatch.setattr(sync, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sync, "CHART", tmp_path / "chart")
    monkeypatch.setattr(sync, "COPIES", {"files/c.template": "config/c.template"})
    for argv in (["--check"], []):
        assert sync.main(argv) == 1
        assert "git checkout -- config/c.template" in capsys.readouterr().err
    assert (tmp_path / "chart" / "files" / "c.template").read_bytes() == b"a: 1\nb: 2\n"


def test_the_sync_script_knows_every_copy_and_reports_none_stale():
    out = subprocess.run([sys.executable, os.path.join(_REPO, "scripts", "sync_lite_chart.py"),
                          "--check"], cwd=_REPO, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    for copy in COPIES:
        assert copy in out.stdout, copy


def test_chart_metadata():
    chart = yaml.safe_load(_read(os.path.join(_CHART, "Chart.yaml")))
    assert chart["apiVersion"] == "v2"
    assert chart["name"] == "tokenlean-lite"
    assert chart["type"] == "application"
    assert re.match(r"^\d+\.\d+\.\d+$", chart["version"])
    assert "dependencies" not in chart          # self-contained: an umbrella chart nests it


@pytest.mark.parametrize("name", _ENVS)
def test_values_files_hold_secret_references_never_secret_values(name):
    for path, value in _leaves(_values(name)):
        leaf = path.rsplit(".", 1)[-1].lower()
        if re.search(r"password|secret|token|apikey|api_key|credential", leaf) and not \
                leaf.endswith(("secretname", "secretkey", "existingsecret")):
            assert value in (None, "", {}, False), f"{name}: {path} holds a value"


def test_chart_defaults_hold_no_secret_values():
    for path, value in _leaves(_chart_values()):
        leaf = path.rsplit(".", 1)[-1].lower()
        if re.search(r"password|secret|token|apikey|api_key|credential", leaf) and not \
                leaf.endswith(("secretname", "secretkey", "existingsecret")):
            assert value in (None, "", {}, False), f"values.yaml: {path} holds a value"


def test_chart_reads_every_name_that_managed_yaml_fixed():
    """values/managed.yaml was written before the chart, as what it would read: every key in
    it is a key of the chart's values, under the same name."""
    chart_keys = set(_key_paths(_chart_values()))
    missing = [k for k in _key_paths(_values("managed")) if k not in chart_keys]
    assert not missing, missing


@pytest.mark.parametrize("name", ["dev", "staging", "prod"])
def test_env_values_only_use_names_the_chart_has(name):
    chart_keys = set(_key_paths(_chart_values()))
    free_form = ("podAnnotations", "podLabels", "nodeSelector", "ingress.annotations",
                 "serviceAccount.annotations", "resources", "affinity")
    unknown = [k for k in _key_paths(_values(name)) if k not in chart_keys
               and not any(k.startswith(f + ".") for f in free_form)]
    assert not unknown, unknown


def test_bundled_redis_is_for_dev_only():
    assert _values("dev")["redis"]["bundled"]["enabled"] is True
    for name in ("staging", "prod", "managed"):
        assert not _values(name).get("redis", {}).get("bundled", {}).get("enabled"), name
    assert _chart_values()["redis"]["bundled"]["enabled"] is False


@pytest.mark.parametrize("path", [os.path.join(_CHART, "Chart.yaml"),
                                  os.path.join(_CHART, "values.yaml")]
                         + [os.path.join(_VALUES_DIR, f"{n}.yaml") for n in _ENVS])
def test_chart_and_values_files_are_lf(path):
    assert b"\r\n" not in _read(path, "rb")


# ── Rendering (helm and kubeconform in pinned containers) ───────────────────────────────

@functools.lru_cache(maxsize=1)
def _engine_or_reason():
    engine = os.environ.get("CONTAINER_ENGINE", "docker")
    if not shutil.which(engine):
        return None, f"no container engine ({engine} not on PATH): helm runs only in a container"
    try:
        info = subprocess.run([engine, "info"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{engine} info failed: {exc}"
    if info.returncode != 0:
        return None, f"{engine} is installed but not running"
    return engine, ""


def _engine():
    engine, reason = _engine_or_reason()
    if not engine:
        pytest.skip(reason)
    return engine


def _run_in(image, args, network="none", timeout=300):
    """Run *image* with deploy/lite/cloud mounted read-only at /work."""
    engine = _engine()
    cmd = [engine, "run", "--rm", "--network", network, "-v", f"{_CLOUD}:/work:ro",
           "-w", "/work", "-e", "HELM_CACHE_HOME=/tmp/helm/cache",
           "-e", "HELM_CONFIG_HOME=/tmp/helm/config", "-e", "HELM_DATA_HOME=/tmp/helm/data",
           image, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8")


@functools.lru_cache(maxsize=None)
def _render_text(env, *extra):
    values = ["-f", f"values/{env}.yaml"] if env else []
    out = _run_in(HELM_IMAGE, ["template", "tokenlean-lite", "helm/tokenlean-lite",
                               "--namespace", "tokenlean", "--kube-version", KUBE_VERSION,
                               *values, *_IMAGE_SET, *extra])
    return out.returncode, out.stdout, out.stderr


def _render(env, *extra):
    code, stdout, stderr = _render_text(env, *extra)
    assert code == 0, stderr
    return [d for d in yaml.safe_load_all(stdout) if d]


def _kind(docs, kind, name_part=""):
    found = [d for d in docs if d["kind"] == kind and name_part in d["metadata"]["name"]]
    return found


def _one(docs, kind, name_part=""):
    found = _kind(docs, kind, name_part)
    assert len(found) == 1, [d["metadata"]["name"] for d in found]
    return found[0]


def _proxy(docs):
    return [d for d in _kind(docs, "Deployment")
            if d["metadata"]["labels"].get("app.kubernetes.io/component") == "proxy"][0]


def _container(deployment, name="proxy"):
    return [c for c in deployment["spec"]["template"]["spec"]["containers"]
            if c["name"] == name][0]


def _env(container):
    return {e["name"]: e for e in container.get("env", [])}


@pytest.mark.parametrize("helm_image", [HELM_IMAGE, HELM3_IMAGE], ids=["helm4", "helm3"])
@pytest.mark.parametrize("env", _ENVS)
def test_helm_lint_passes(helm_image, env):
    out = _run_in(helm_image, ["lint", "--strict", "helm/tokenlean-lite", "-f",
                               f"values/{env}.yaml", *_IMAGE_SET])
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.parametrize("env", _ENVS)
def test_kubeconform_accepts_every_render(env, tmp_path):
    _kubeconform(tmp_path, env)


def test_kubeconform_accepts_a_postgres_ca_and_extra_volumes(tmp_path):
    _kubeconform(tmp_path, "prod", "--set", "postgres.caCertSecretKey=ca.crt",
                 "--set", "proxy.extraVolumes[0].name=corp-ca",
                 "--set", "proxy.extraVolumes[0].configMap.name=corp-ca-bundle",
                 "--set", "proxy.extraVolumeMounts[0].name=corp-ca",
                 "--set", "proxy.extraVolumeMounts[0].mountPath=/etc/corp-ca")


def _kubeconform(tmp_path, env, *extra):
    code, stdout, stderr = _render_text(env, "--set", "metrics.serviceMonitor.enabled=false",
                                        *extra)
    assert code == 0, stderr
    manifest = tmp_path / "rendered.yaml"
    manifest.write_text(stdout, encoding="utf-8")
    engine = _engine()
    out = subprocess.run(
        [engine, "run", "--rm", "-v", f"{tmp_path}:/m:ro", KUBECONFORM_IMAGE,
         "-strict", "-summary", "-kubernetes-version", KUBE_VERSION, "/m/rendered.yaml"],
        capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.parametrize("env", _ENVS)
def test_no_secret_object_and_no_secret_value_is_rendered(env):
    docs = _render(env)
    assert not _kind(docs, "Secret"), "the chart references Secrets; it never creates one"
    for d in docs:
        for c in (d.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                  + d.get("spec", {}).get("template", {}).get("spec", {}).get(
                      "initContainers", [])):
            for e in c.get("env", []):
                if re.search(r"PASSWORD|TOKEN|SECRET|LLM_KEY_|CA_CERT", e["name"]):
                    assert "value" not in e or e["name"] == "LLM_KEY_LITEMOCK", (env, e)


@pytest.mark.parametrize("env", _ENVS)
def test_the_configmap_carries_the_lite_profile_byte_for_byte(env):
    cm = _one(_render(env), "ConfigMap", "-config")
    assert cm["data"]["agentic.yaml"].encode("utf-8") == \
        _read(os.path.join(_LITE, "config", "agentic.yaml"), "rb")
    assert cm["data"]["config.yaml.template"].encode("utf-8") == \
        _read(os.path.join(_REPO, "config", "config.yaml.template"), "rb")


@pytest.mark.parametrize("env", _ENVS)
def test_the_proxy_applies_the_lite_profile_first(env):
    proxy = _container(_proxy(_render(env)))
    env_vars = _env(proxy)
    overlays = env_vars["CONFIG_OVERLAYS"]["value"].split(",")
    assert overlays[0] == "/app/lite/config/agentic.yaml"
    assert env_vars["CONFIG_LOCAL_PATH"]["value"] == "/app/config/config.yaml.template"
    assert env_vars["STORAGE_BACKEND"]["value"] == "local"
    assert env_vars["LOCAL_PROXY_KEYS_FILE"]["value"] == "/app/lite/state/local-keys.json"
    assert env_vars["METRICS_ALLOW_UNAUTHENTICATED"]["value"] == "false"
    assert env_vars["MANAGED_DEPLOY"]["value"] == "true"   # serve only once the DB is wired
    mounts = {m["mountPath"]: m for m in proxy["volumeMounts"]}
    for path in ("/app/config", "/app/lite/config", "/app/lite/state"):
        assert mounts[path].get("readOnly") is True, path


def test_the_env_template_and_the_chart_set_the_same_proxy_variables():
    """The container environment of tokenlean-lite.env.template is what the chart renders."""
    template = {line.split("=", 1)[0] for line in
                _read(os.path.join(_CLOUD, "tokenlean-lite.env.template")).splitlines()
                if line and not line.startswith("#")}
    proxy = _container(_proxy(_render("managed")))
    rendered = set(_env(proxy))
    # Provider keys come in as a whole Secret (envFrom); CORS_ORIGINS only when set.
    expected = {k for k in template if not k.startswith("LLM_KEY_")} - {"CORS_ORIGINS",
                                                                        "REDIS_CA_CERT"}
    missing = expected - rendered
    assert not missing, sorted(missing)
    assert {"secretRef": {"name": "tokenlean-lite-provider-keys"}} in proxy["envFrom"]


@pytest.mark.parametrize("env", _ENVS)
def test_probes_use_the_health_endpoint(env):
    proxy = _container(_proxy(_render(env)))
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        get = proxy[probe]["httpGet"]
        assert get["path"] == "/health" and get["port"] == "http", (probe, get)
    assert proxy["ports"][0] == {"name": "http", "containerPort": 8080, "protocol": "TCP"}


@pytest.mark.parametrize("env", _ENVS)
def test_every_pod_runs_non_root_with_a_read_only_root(env):
    docs = _render(env)
    for d in docs:
        if d["kind"] not in ("Deployment", "StatefulSet", "Job"):
            continue
        spec = d["spec"]["template"]["spec"]
        assert spec["securityContext"]["runAsNonRoot"] is True, d["metadata"]["name"]
        assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        assert spec.get("automountServiceAccountToken") is False, d["metadata"]["name"]
        for c in spec["containers"]:
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False, c["name"]
            assert sc["readOnlyRootFilesystem"] is True, c["name"]
            assert sc["capabilities"]["drop"] == ["ALL"], c["name"]
            assert c.get("resources", {}).get("requests"), c["name"]


def test_the_proxy_runs_as_the_image_user():
    spec = _proxy(_render("prod"))["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsUser"] == 1000   # the image's `app` user
    tmp = [v for v in spec["volumes"] if v["name"] == "tmp"][0]
    assert "emptyDir" in tmp       # PROMETHEUS_MULTIPROC_DIR and scratch, on a read-only root


def test_external_postgres_password_comes_from_its_secret_never_the_url():
    proxy = _container(_proxy(_render("managed")))
    env_vars = _env(proxy)
    url = env_vars["DATABASE_URL"]["value"]
    assert url == ("postgresql://tokenlean@postgres.example.internal:5432/tokenlean"
                   "?sslmode=require")
    assert env_vars["PGPASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "tokenlean-lite-postgres", "key": "password"}


@pytest.mark.parametrize("env", ["staging", "prod", "managed"])
def test_external_redis_over_tls(env):
    docs = _render(env)
    assert not _kind(docs, "StatefulSet")
    env_vars = _env(_container(_proxy(docs)))
    assert env_vars["REDIS_URL"]["value"].startswith("rediss://")
    assert env_vars["REDIS_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == "password"


def test_bundled_redis_renders_for_dev_and_the_proxy_uses_it():
    docs = _render("dev")
    redis = _one(docs, "StatefulSet", "redis")
    image = redis["spec"]["template"]["spec"]["containers"][0]["image"]
    compose = yaml.safe_load(_read(os.path.join(_LITE, "local", "docker-compose.lite.yml")))
    assert image == compose["services"]["redis"]["image"]     # one pin with the local stack
    _one(docs, "Service", "redis")
    env_vars = _env(_container(_proxy(docs)))
    assert env_vars["REDIS_URL"]["value"] == "redis://tokenlean-lite-redis:6379/0"


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_autoscaled_envs_render_an_hpa_and_leave_replicas_to_it(env):
    docs = _render(env)
    hpa = _one(docs, "HorizontalPodAutoscaler")
    assert hpa["apiVersion"] == "autoscaling/v2"
    assert hpa["spec"]["minReplicas"] >= 2
    assert hpa["spec"]["scaleTargetRef"]["name"] == _proxy(docs)["metadata"]["name"]
    assert "replicas" not in _proxy(docs)["spec"]
    pdb = _one(docs, "PodDisruptionBudget")
    assert pdb["apiVersion"] == "policy/v1"


def test_dev_runs_one_replica_without_an_hpa():
    docs = _render("dev")
    assert not _kind(docs, "HorizontalPodAutoscaler")
    assert _proxy(docs)["spec"]["replicas"] == 1


@pytest.mark.parametrize("env", ["dev", "staging", "prod", "managed"])
def test_ingress_has_tls_and_names_the_host(env):
    docs = _render(env)
    ingress = _one(docs, "Ingress")
    host = ingress["spec"]["rules"][0]["host"]
    assert ingress["spec"]["tls"][0]["hosts"] == [host]
    assert ingress["spec"]["tls"][0]["secretName"]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_cert_manager_issues_the_certificate(env):
    ingress = _one(_render(env), "Ingress")
    assert ingress["metadata"]["annotations"].get("cert-manager.io/cluster-issuer")


def test_ingress_is_off_unless_asked():
    assert not _kind(_render(""), "Ingress")


@pytest.mark.parametrize("env", _ENVS)
def test_prometheus_scrape_annotations(env):
    meta = _proxy(_render(env))["spec"]["template"]["metadata"]
    annotations = meta["annotations"]
    assert annotations["prometheus.io/scrape"] == "true"
    assert annotations["prometheus.io/path"] == "/metrics"
    assert annotations["prometheus.io/port"] == "8080"
    assert re.match(r"^[0-9a-f]{64}$", annotations["checksum/config"])


def test_service_monitor_presents_the_scrape_token():
    docs = _render("managed", "--set", "metrics.serviceMonitor.enabled=true")
    monitor = _one(docs, "ServiceMonitor")
    endpoint = monitor["spec"]["endpoints"][0]
    assert endpoint["authorization"]["credentials"] == {
        "name": "tokenlean-lite-metrics", "key": "scrape-token"}


@pytest.mark.parametrize("env", ["staging", "prod", "managed"])
def test_a_migration_hook_runs_the_schema_job_before_install_and_upgrade(env):
    job = _one(_render(env), "Job")
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert "before-hook-creation" in annotations["helm.sh/hook-delete-policy"]
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert "schema_setup" in " ".join(container["command"] + container.get("args", []))
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    assert _env(container)["PGPASSWORD"]["valueFrom"]["secretKeyRef"]


def test_the_migration_hook_can_use_an_owner_role():
    job = _one(_render("managed", "--set", "migrations.user=tokenlean_owner",
                       "--set", "migrations.existingSecret=tokenlean-lite-postgres-owner"),
               "Job")
    env_vars = _env(job["spec"]["template"]["spec"]["containers"][0])
    assert env_vars["DATABASE_URL"]["value"].startswith("postgresql://tokenlean_owner@")
    assert env_vars["PGPASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == \
        "tokenlean-lite-postgres-owner"


_PG_CA_FILE = "/etc/tokenlean-lite/postgres-ca/ca.crt"


def _pod_specs(docs):
    """The proxy's and the migration hook's pod specs, with their one container."""
    job = _one(docs, "Job")["spec"]["template"]["spec"]
    proxy = _proxy(docs)["spec"]["template"]["spec"]
    return {"proxy": (proxy, _container(_proxy(docs))), "migrate": (job, job["containers"][0])}


def test_a_private_postgres_ca_reaches_the_proxy_and_the_migration_hook():
    """verify-full against a managed Postgres signed by a private CA: the CA comes from the
    Postgres Secret as a file, and PGSSLROOTCERT (read by the driver) names it."""
    docs = _render("prod", "--set", "postgres.caCertSecretKey=ca.crt")
    for name, (spec, container) in _pod_specs(docs).items():
        volume = [v for v in spec["volumes"] if v["name"] == "postgres-ca"]
        assert volume and volume[0]["secret"] == {
            "secretName": "tokenlean-lite-postgres",
            "items": [{"key": "ca.crt", "path": "ca.crt"}]}, (name, spec["volumes"])
        mounts = {m["name"]: m for m in container["volumeMounts"]}
        assert mounts["postgres-ca"]["mountPath"] == os.path.dirname(_PG_CA_FILE), name
        assert mounts["postgres-ca"]["readOnly"] is True, name
        assert _env(container)["PGSSLROOTCERT"]["value"] == _PG_CA_FILE, name


@pytest.mark.parametrize("env", _ENVS)
def test_without_a_postgres_ca_the_system_trust_store_is_used(env):
    docs = _render(env)
    for name, (spec, container) in _pod_specs(docs).items():
        assert "PGSSLROOTCERT" not in _env(container), name
        assert not [v for v in spec["volumes"] if v["name"] == "postgres-ca"], name


def test_extra_volumes_and_mounts_reach_the_proxy():
    docs = _render("prod", "--set", "proxy.extraVolumes[0].name=corp-ca",
                   "--set", "proxy.extraVolumes[0].configMap.name=corp-ca-bundle",
                   "--set", "proxy.extraVolumeMounts[0].name=corp-ca",
                   "--set", "proxy.extraVolumeMounts[0].mountPath=/etc/corp-ca",
                   "--set", "proxy.extraVolumeMounts[0].readOnly=true")
    spec = _proxy(docs)["spec"]["template"]["spec"]
    assert {"name": "corp-ca", "configMap": {"name": "corp-ca-bundle"}} in spec["volumes"]
    mounts = _container(_proxy(docs))["volumeMounts"]
    assert {"name": "corp-ca", "mountPath": "/etc/corp-ca", "readOnly": True} in mounts


def test_the_mock_provider_is_off_by_default_and_matches_the_local_overlay():
    assert not _kind(_render("dev"), "Deployment", "mock")
    docs = _render("dev", "--set", "mockProvider.enabled=true")
    mock = _one(docs, "Deployment", "mock")
    _one(docs, "Service", "mock")
    assert _container(mock, "mock-llm")["command"][-1] == "/app/lite/mock/mock_llm.py"
    cm = _one(docs, "ConfigMap", "-config")
    rendered = yaml.safe_load(cm["data"]["mock-provider.yaml"])
    local = yaml.safe_load(_read(os.path.join(_LITE, "local", "mock", "mock-provider.yaml")))
    assert rendered["providers"][0].pop("api_base") == \
        "http://tokenlean-lite-mock:8080/v1"
    local["providers"][0].pop("api_base")
    assert rendered == local
    overlays = _env(_container(_proxy(docs)))["CONFIG_OVERLAYS"]["value"].split(",")
    assert overlays[-1] == "/app/lite/config/mock-provider.yaml"


def test_overlays_are_applied_in_order_after_the_profile():
    docs = _render("managed", "--set", "config.overlays[0]=deploy/lite/config/langfuse.yaml",
                   "--set", "config.extraOverlays[0].name=team-limits.yaml",
                   "--set", "config.extraOverlays[0].content=rate_limit: {}")
    overlays = _env(_container(_proxy(docs)))["CONFIG_OVERLAYS"]["value"].split(",")
    assert overlays == ["/app/lite/config/agentic.yaml", "/app/lite/config/langfuse.yaml",
                        "/app/lite/config/team-limits.yaml"]
    cm = _one(docs, "ConfigMap", "-config")
    assert cm["data"]["langfuse.yaml"].encode() == \
        _read(os.path.join(_LITE, "config", "langfuse.yaml"), "rb")
    assert cm["data"]["team-limits.yaml"] == "rate_limit: {}"


@pytest.mark.parametrize("bad, message", [
    (("--set", "image.tag=latest"), "image.tag"),
    (("--set", "image.tag=set-me"), "image.tag"),
    (("--set", "config.overlays[0]=deploy/lite/config/nope.yaml"), "config.overlays"),
    (("--set", "config.profile=config/other.yaml"), "config.profile"),
    (("--set", "proxyKeys.backend=file"), "proxyKeys.backend"),
    (("--set", "postgres.existingSecret="), "postgres.existingSecret"),
])
def test_unsafe_values_fail_the_render_with_a_reason(bad, message):
    code, _stdout, stderr = _render_text("managed", *bad)
    assert code != 0
    assert message in stderr, stderr


@pytest.mark.parametrize("env", _ENVS)
def test_every_image_is_pinned_to_an_exact_release(env):
    for d in _render(env):
        spec = d.get("spec", {}).get("template", {}).get("spec", {})
        for c in spec.get("containers", []):
            repo, _, tag = c["image"].rpartition(":")
            assert repo and re.match(r"^v?\d+\.\d+\.\d+", tag), c["image"]


def test_the_kind_values_render_over_dev_with_the_mock():
    """kind/values.yaml (e2e.sh): the throwaway Postgres, no ingress, the loaded image."""
    docs = _render("dev", "-f", "kind/values.yaml", "--set", "mockProvider.enabled=true")
    assert not _kind(docs, "Ingress")
    env_vars = _env(_container(_proxy(docs)))
    assert env_vars["DATABASE_URL"]["value"] == (
        "postgresql://tokenlean@tokenlean-lite-dev-postgres:5432/tokenlean?sslmode=disable")
    _one(docs, "Deployment", "mock")
    _one(docs, "StatefulSet", "redis")


@pytest.mark.skipif(os.environ.get("TOKENLEAN_LITE_KIND_E2E") != "1",
                    reason="slow (a kind cluster): set TOKENLEAN_LITE_KIND_E2E=1 and "
                           "TOKENLEAN_IMAGE to a proxy image built from src/proxy")
def test_kind_end_to_end():
    """install, smoke, upgrade, smoke, uninstall --purge on a local kind cluster, keyless."""
    _engine()
    bash = shutil.which("bash")
    if sys.platform == "win32" and os.path.exists(r"C:\Program Files\Git\bin\bash.exe"):
        bash = r"C:\Program Files\Git\bin\bash.exe"
    image = os.environ.get("TOKENLEAN_IMAGE", "")
    assert image, "TOKENLEAN_IMAGE must name a proxy image built from src/proxy"
    out = subprocess.run([bash, "./e2e.sh", "--image", image], cwd=os.path.join(_CLOUD, "kind"),
                         capture_output=True, text=True, timeout=3600)
    assert out.returncode == 0, out.stdout[-6000:] + out.stderr[-3000:]
    assert "e2e: all steps passed" in out.stdout


@pytest.mark.parametrize("env", _ENVS)
def test_names_are_namespaced_by_the_release(env):
    """An umbrella chart nests this chart: every object carries the release's labels and a
    name that starts with the release's fullname, so two releases never collide."""
    docs = _render(env)
    for d in docs:
        assert d["metadata"]["name"].startswith("tokenlean-lite"), d["metadata"]["name"]
        labels = d["metadata"]["labels"]
        assert labels["app.kubernetes.io/instance"] == "tokenlean-lite"
        assert labels["app.kubernetes.io/name"] == "tokenlean-lite"
    renamed = _render(env, "--set", "fullnameOverride=edge-gateway")
    assert all(d["metadata"]["name"].startswith("edge-gateway") for d in renamed)
