#!/usr/bin/env bash
# setup.sh — check/install/upgrade everything paymemoney needs, then start minikube.
#
# Safe to re-run: it only installs what's missing and only upgrades tools
# when Homebrew actually manages them and reports them outdated.
#
#   ./setup.sh            # check + fix + start cluster
#   ./setup.sh --no-start # check + fix only, don't start minikube

set -euo pipefail

START_MINIKUBE=1
[[ "${1:-}" == "--no-start" ]] && START_MINIKUBE=0

# name | brew formula | why we need it
REQUIREMENTS=(
  "minikube|minikube|local kubernetes cluster"
  "kubectl|kubectl|talks to the cluster"
  "python3|python|used for `python3 -m py_compile` sanity checks"
)

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# brew considers a formula "installed" only if it appears in `brew list`.
brew_has() { brew list --formula "$1" >/dev/null 2>&1 || brew list --cask "$1" >/dev/null 2>&1; }

# --- package manager ---------------------------------------------------------
if ! command -v brew >/dev/null; then
  die "Homebrew not found. Install it first: https://brew.sh"
fi
ok "brew $(brew --version | head -1 | awk '{print $2}')"

# quiet refresh so `brew outdated` below is accurate (once per day is enough)
brew update --quiet >/dev/null 2>&1 || warn "brew update failed (offline?), continuing anyway"

# --- docker -------------------------------------------------------------------
# Docker comes as Docker Desktop (cask), not the `docker` formula.
# What matters is the daemon, not just the CLI.
if ! command -v docker >/dev/null; then
  log "docker CLI missing — installing Docker Desktop (brew cask)"
  brew install --cask docker
fi
if ! docker info >/dev/null 2>&1; then
  log "docker daemon not running — starting Docker Desktop"
  open -a Docker 2>/dev/null || die "cannot start Docker Desktop; open it manually"
  for _ in $(seq 1 60); do
    docker info >/dev/null 2>&1 && break
    sleep 2
  done
  docker info >/dev/null 2>&1 || die "docker daemon did not come up in 2 minutes"
fi
ok "docker daemon running"

# --- check / install / upgrade each tool -------------------------------------
for entry in "${REQUIREMENTS[@]}"; do
  IFS='|' read -r name formula why <<<"$entry"
  if command -v "$name" >/dev/null; then
    # Only brew-upgrade when brew actually owns this install.
    # (e.g. Apple's system tools — leave them alone.)
    if brew_has "$formula" && brew outdated --quiet "$formula" >/dev/null 2>&1; then
      log "$name is outdated — upgrading ($formula)"
      brew upgrade "$formula"
    else
      ok "$name"
    fi
  else
    log "$name missing — installing ($formula). $why"
    brew install "$formula"
  fi
done

# --- minikube -----------------------------------------------------------------
if [[ $START_MINIKUBE -eq 1 ]]; then
  if minikube status >/dev/null 2>&1; then
    ok "minikube already running ($(minikube ip 2>/dev/null || echo '?'))"
  else
    log "starting minikube (first start downloads a VM image, can take a while)"
    minikube start --driver=docker
    ok "minikube running at $(minikube ip)"
  fi
else
  warn "skipped minikube start (--no-start)"
fi

cat <<'EOF'

All set. Next steps:

  ./pmm secret sk_test_xxx pk_test_yyy  # inject both Stripe test keys
  ./pmm deploy                          # build + apply + restart pods
  ./pmm ui                             # open http://localhost:8000 in your browser

See README.md for the full guide (`./pmm help` lists all commands).
EOF
