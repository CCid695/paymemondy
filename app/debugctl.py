"""Debug controller — the backend for the "Deploy" button in the web UI.

The web pod has no kubectl binary and no Kubernetes credentials, so it cannot
redeploy or reconfigure the debug backends itself. This tiny pod can: it runs
with a ServiceAccount (`debug-ctl`, see k8s/rbac.yaml) that has patch rights on
Deployments and Services in the paymemoney namespace, and exposes:

    GET  /health
    POST /deploy?mode=debug-api   -> pick a RANDOM PAY_BREAK scenario, patch the
                                     debug-api deployment (env change triggers
                                     an automatic rollout restart)
    POST /deploy?mode=debug-k8s   -> pick a RANDOM k8s-level fault, patch the
                                     live debug-k8s Service/Deployment objects

The scenarios are the same ones formerly driven by `./pmm break-api` and
`break-k8s.sh` — now chosen by a random number so you can practice forever
without touching the CLI. The committed manifests stay correct; only live
objects are patched.
"""

import os
import random

from fastapi import FastAPI
from kubernetes import client, config

NS = "paymemoney"

app = FastAPI(title="paymemoney-debug-ctl")

# API-level faults injected via the PAY_BREAK env var on the debug-api pod.
# The branches live in app/pay.py next to PAY_BREAK — read them to learn what
# each one breaks (encoding / routing / auth / silent wrong amount / healthy).
API_SCENARIOS = ["json-body", "wrong-path", "no-auth", "bad-amount", "none"]

# K8s-level faults patched onto the LIVE debug-k8s service/deployment.
K8S_SCENARIOS = ["selector", "port", "probe", "scale0", "reset"]

_apps = _core = None


def k8s_clients():
    """Lazy-init the Kubernetes client (in-cluster inside the pod)."""
    global _apps, _core
    if _apps is None:
        try:
            config.load_incluster_config()   # running inside k8s: uses SA token
        except config.ConfigException:
            config.load_kube_config()        # local dev fallback
        _apps = client.AppsV1Api()
        _core = client.CoreV1Api()
    return _apps, _core


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def deploy_api() -> dict:
    """Pick a random PAY_BREAK and patch it onto the debug-api deployment."""
    target = random.choice(API_SCENARIOS)
    apps, _ = k8s_clients()
    deploy = apps.read_namespaced_deployment("debug-api", NS)
    for container in deploy.spec.template.spec.containers:
        for env in container.env or []:
            if env.name == "PAY_BREAK":
                env.value = target
    apps.replace_namespaced_deployment("debug-api", NS, deploy)
    return {
        "mode": "debug-api",
        "scenario": target,
        "detail": "PAY_BREAK env patched — the pod is rolling out now",
    }


def _debug_k8s_service():
    _, core = k8s_clients()
    return core.read_namespaced_service("debug-k8s", NS)


def _debug_k8s_deploy():
    apps, _ = k8s_clients()
    return apps.read_namespaced_deployment("debug-k8s", NS)


def deploy_k8s() -> dict:
    """Pick a random k8s fault and patch the live debug-k8s objects."""
    target = random.choice(K8S_SCENARIOS)
    apps, core = k8s_clients()

    if target == "selector":
        # Service selector matches no pod labels -> zero endpoints.
        svc = _debug_k8s_service()
        svc.spec.selector = {"app.kubernetes.io/name": "debug-k8s-broken"}
        core.replace_namespaced_service("debug-k8s", NS, svc)

    elif target == "port":
        # Service forwards to containerPort 9999 where nothing listens.
        svc = _debug_k8s_service()
        svc.spec.ports[0].target_port = 9999
        core.replace_namespaced_service("debug-k8s", NS, svc)

    elif target == "probe":
        # Probes hit /broken -> CrashLoopBackOff / never Ready.
        deploy = _debug_k8s_deploy()
        for c in deploy.spec.template.spec.containers:
            if c.liveness_probe and c.liveness_probe.http_get:
                c.liveness_probe.http_get.path = "/broken"
            if c.readiness_probe and c.readiness_probe.http_get:
                c.readiness_probe.http_get.path = "/broken"
        apps.replace_namespaced_deployment("debug-k8s", NS, deploy)

    elif target == "scale0":
        # No pods at all.
        scale = client.V1Scale(spec=client.V1ScaleSpec(replicas=0))
        apps.patch_namespaced_scale("debug-k8s", NS, scale)

    else:  # reset — restore the healthy baseline
        svc = _debug_k8s_service()
        svc.spec.selector = {"app.kubernetes.io/name": "debug-k8s"}
        svc.spec.ports[0].target_port = "http"
        core.replace_namespaced_service("debug-k8s", NS, svc)

        deploy = _debug_k8s_deploy()
        deploy.spec.replicas = 1
        for c in deploy.spec.template.spec.containers:
            if c.liveness_probe and c.liveness_probe.http_get:
                c.liveness_probe.http_get.path = "/health"
            if c.readiness_probe and c.readiness_probe.http_get:
                c.readiness_probe.http_get.path = "/health"
        apps.replace_namespaced_deployment("debug-k8s", NS, deploy)

    return {
        "mode": "debug-k8s",
        "scenario": target,
        "detail": "live objects patched — give the rollout a few seconds",
    }


@app.post("/deploy")
def deploy(mode: str) -> dict:
    if mode == "debug-api":
        return deploy_api()
    if mode == "debug-k8s":
        return deploy_k8s()
    return {"error": f"unknown mode: {mode} (want debug-api or debug-k8s)"}
