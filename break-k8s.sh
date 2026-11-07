#!/usr/bin/env bash
# break-k8s.sh — introduce a K8s-level fault into the debug-k8s pod/service
# so you can practice cluster troubleshooting. Patches LIVE objects only;
# the committed manifests stay correct.
#
#   ./break-k8s.sh selector   # svc selector matches nothing -> 0 endpoints
#   ./break-k8s.sh port       # svc targetPort wrong -> endpoints up, conn refused
#   ./break-k8s.sh probe      # probes hit /broken -> CrashLoopBackOff
#   ./break-k8s.sh scale0     # scale deployment to 0 -> no pods at all
#   ./break-k8s.sh reset      # re-apply correct manifests -> fully healthy
#
# Troubleshooting hints are printed after each break.

set -euo pipefail
NS=paymemoney
TARGET="${1:-}"

case "$TARGET" in
  selector)
    kubectl patch svc debug-k8s -n "$NS" --type=json -p \
      '[{"op":"replace","path":"/spec/selector","value":{"app.kubernetes.io/name":"debug-k8s-broken"}}]'
    echo "BROKEN: service selector matches no pods."
    echo "  Try: kubectl get endpoints debug-k8s -n $NS   (empty!)"
    ;;
  port)
    kubectl patch svc debug-k8s -n "$NS" --type=json -p \
      '[{"op":"replace","path":"/spec/ports/0/targetPort","value":9999}]'
    echo "BROKEN: service forwards to containerPort 9999 (nothing listens)."
    echo "  Try: kubectl get endpoints debug-k8s -n $NS   (shows podIP:9999)"
    ;;
  probe)
    kubectl patch deploy debug-k8s -n "$NS" --type=json -p \
      '[{"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/httpGet/path","value":"/broken"},
        {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/broken"}]'
    echo "BROKEN: liveness+readiness probes hit /broken -> pods restart / never Ready."
    echo "  Try: kubectl get pods -n $NS   then  kubectl describe pod <debug-k8s-pod>"
    ;;
  scale0)
    kubectl scale deploy/debug-k8s -n "$NS" --replicas=0
    echo "BROKEN: deployment scaled to 0 replicas — no pods exist."
    echo "  Try: kubectl get pods,svc,endpoints -n $NS"
    ;;
  reset)
    kubectl apply -f k8s/debug-k8s-service.yaml -f k8s/debug-k8s-deployment.yaml
    kubectl rollout status deploy/debug-k8s -n "$NS" --timeout=60s
    echo "HEALED: debug-k8s is healthy again."
    ;;
  *)
    echo "usage: $0 {selector|port|probe|scale0|reset}" >&2
    exit 1
    ;;
esac
