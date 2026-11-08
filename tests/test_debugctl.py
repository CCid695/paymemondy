"""Unit tests for app/debugctl.py — the Deploy-button backend.

The kubernetes client calls are faked; no cluster is touched.
"""

import app.debugctl as ctl
from fastapi.testclient import TestClient

client = TestClient(ctl.app)


class FakeDeploy:
    """Minimal stand-in for a k8s Deployment object."""
    pass


def _fake_deploy_with_env(value="json-body"):
    env = type("Env", (), {"name": "PAY_BREAK", "value": value})()
    container = type("C", (), {"env": [env]})()
    spec = type("S", (), {"template": type("T", (), {"spec": type("P", (), {"containers": [container]})()})()})()
    return type("D", (), {"spec": spec})()


class FakeApps:
    def __init__(self):
        self.replaced = []
        self.patched_scale = None

    def read_namespaced_deployment(self, name, ns):
        return _fake_deploy_with_env()

    def replace_namespaced_deployment(self, name, ns, deploy):
        self.replaced.append((name, deploy))

    def patch_namespaced_scale(self, name, ns, scale):
        self.patched_scale = scale


class FakeCore:
    def read_namespaced_service(self, name, ns):
        port = type("P", (), {"target_port": "http"})()
        svc = type("S", (), {"spec": type("SP", (), {
            "selector": {"app.kubernetes.io/name": "debug-k8s"},
            "ports": [port],
        })()})()
        return svc

    def replace_namespaced_service(self, name, ns, svc):
        self.last_svc = svc


def _patch_clients(monkeypatch):
    apps, core = FakeApps(), FakeCore()
    monkeypatch.setattr(ctl, "k8s_clients", lambda: (apps, core))
    return apps, core


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_deploy_api_patches_pay_break(monkeypatch):
    apps, _ = _patch_clients(monkeypatch)
    monkeypatch.setattr(ctl.random, "choice", lambda seq: "no-auth")
    resp = client.post("/deploy?mode=debug-api")
    assert resp.json()["scenario"] == "no-auth"
    # the deployment was replaced with PAY_BREAK=no-auth
    env = apps.replaced[0][1].spec.template.spec.containers[0].env[0]
    assert env.value == "no-auth"


def test_deploy_k8s_selector_fault(monkeypatch):
    _, core = _patch_clients(monkeypatch)
    monkeypatch.setattr(ctl.random, "choice", lambda seq: "selector")
    resp = client.post("/deploy?mode=debug-k8s")
    assert resp.json()["scenario"] == "selector"
    assert core.last_svc.spec.selector["app.kubernetes.io/name"] == "debug-k8s-broken"


def test_deploy_k8s_scale0_fault(monkeypatch):
    apps, _ = _patch_clients(monkeypatch)
    monkeypatch.setattr(ctl.random, "choice", lambda seq: "scale0")
    resp = client.post("/deploy?mode=debug-k8s")
    assert resp.json()["scenario"] == "scale0"
    assert apps.patched_scale.spec.replicas == 0


def test_deploy_unknown_mode():
    assert "unknown mode" in client.post("/deploy?mode=bogus").json()["error"]
