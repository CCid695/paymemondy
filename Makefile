IMAGE ?= paymemoney:dev
NAMESPACE ?= paymemoney
LOCAL_PORT ?= 8000

.PHONY: minikube-start build load deploy secret test break-api break-k8s status logs tunnel port-forward ui ui-stop delete

# Inject the Stripe key without writing it to any repo file:
#   make secret STRIPE_SECRET_KEY=sk_test_xxxxxxxx
# The key only exists in your shell history and the cluster Secret.
secret:
ifndef STRIPE_SECRET_KEY
	$(error STRIPE_SECRET_KEY is not set. Usage: make secret STRIPE_SECRET_KEY=sk_test_...)
endif
	kubectl create secret generic stripe-secret -n $(NAMESPACE) \
		--from-literal=STRIPE_SECRET_KEY=$(STRIPE_SECRET_KEY) \
		--dry-run=client -o yaml | kubectl apply -f -
	kubectl rollout restart deployment/payment -n $(NAMESPACE)

minikube-start:
	minikube start --driver=docker

build:
	docker build -t $(IMAGE) .

load:
	minikube image load $(IMAGE)

deploy: build load
	kubectl apply -k k8s
	# image tag never changes, so restart pods to pick up the new image
	kubectl rollout restart deployment -n $(NAMESPACE)

# Unit tests — pytest, no cluster or Stripe needed.
# Dev deps (pytest) live in requirements-dev.txt, NOT in the Docker image.
# Uses .venv (python3.15) — system python3 is too old for the app deps.
test:
	@test -x .venv/bin/python || /opt/homebrew/bin/python3.15 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt
	.venv/bin/python -m pytest tests/ -v

# Debug knobs — move the intentional bug around for troubleshooting practice.
# API-level bugs (inside the debug-api pod):
#   make break-api TARGET=json-body|wrong-path|no-auth|bad-amount|none
break-api:
	kubectl set env deployment/debug-api -n $(NAMESPACE) PAY_BREAK=$(TARGET)

# K8s-level bugs (live cluster only, manifests stay correct):
#   make break-k8s TARGET=selector|port|probe|scale0|reset
break-k8s:
	./break-k8s.sh $(TARGET)

status:
	kubectl get pods,svc -n $(NAMESPACE)

logs:
	kubectl logs -n $(NAMESPACE) -l app.kubernetes.io/name=paymemoney --tail=100 -f

tunnel:
	minikube service paymemoney -n $(NAMESPACE)

port-forward:
	kubectl port-forward -n $(NAMESPACE) svc/paymemoney $(LOCAL_PORT):80

# One command: background port-forward + open the browser.
# The forward keeps running after make exits; stop it with `make ui-stop`.
ui:
	@nohup kubectl port-forward -n $(NAMESPACE) svc/paymemoney $(LOCAL_PORT):80 \
		>/tmp/paymemoney-portforward.log 2>&1 & \
	sleep 2 && open "http://localhost:$(LOCAL_PORT)" && \
	echo "opened http://localhost:$(LOCAL_PORT) — stop the forward with: make ui-stop"

ui-stop:
	@if pkill -f "port-forward.*svc/paymemoney"; then \
		echo "port-forward stopped"; \
	else \
		echo "no port-forward running"; \
	fi

delete:
	kubectl delete -k k8s
	kubectl delete namespace $(NAMESPACE) --ignore-not-found
