#!/bin/bash
set -euo pipefail

ready_timeout_seconds="${READY_TIMEOUT_SECONDS:-300}"
if ! [[ "$ready_timeout_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "READY_TIMEOUT_SECONDS must be a positive integer." >&2
  exit 1
fi

echo "Waiting for cert-manager, cainjector, and webhook rollouts..."
for deployment in cert-manager cert-manager-cainjector cert-manager-webhook; do
  kubectl rollout status "deployment/$deployment" -n cert-manager \
    --timeout="${ready_timeout_seconds}s"
done

# A running webhook Pod is not enough: the API server must trust its CA and
# reach its Service. A server-side dry run exercises admission without issuing
# a certificate or requiring the HTTP-01 ingress controller we install below.
echo "Waiting for the cert-manager webhook to accept Certificate requests..."
deadline=$((SECONDS + ready_timeout_seconds))
until kubectl create --dry-run=server --request-timeout=10s -f - >/dev/null <<'YAML'
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  generateName: ingress-readiness-
  namespace: cert-manager
spec:
  secretName: ingress-readiness-tls
  dnsNames:
    - readiness.example.invalid
  issuerRef:
    name: ingress-readiness
    kind: Issuer
YAML
do
  if (( SECONDS >= deadline )); then
    echo "cert-manager API did not become ready; NGINX installation stopped." >&2
    exit 1
  fi
  echo "cert-manager webhook is not ready yet; retrying in 5 seconds..."
  sleep 5
done

echo "Installing NGINX Ingress Controller..."

# Add the official ingress-nginx Helm repository
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update

# Install the NGINX Ingress Controller
# This explicitly creates a Layer 4 LoadBalancer (TCP pass-through for ports 80 and 443)
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --version "${INGRESS_NGINX_CHART_VERSION:-4.10.0}" \
  --wait --wait-for-jobs --timeout="${ready_timeout_seconds}s" \
  --set controller.service.type=LoadBalancer \
  --set controller.service.ports.http=80 \
  --set controller.service.ports.https=443 \
  --set controller.service.targetPorts.http=http \
  --set controller.service.targetPorts.https=https \
  --set controller.service.externalTrafficPolicy=Cluster \
  --set controller.metrics.enabled=true

kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx \
  --timeout="${ready_timeout_seconds}s"

# -----------------------------------------------------------------------------
# AWS NOTE: If you are running on AWS (EKS) and want to force a Network Load Balancer (NLB),
# you would uncomment the following lines and add them to the helm command above:
# 
#  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"="nlb"
# -----------------------------------------------------------------------------

echo "NGINX Ingress Controller is ready. LoadBalancer address:"
kubectl get service ingress-nginx-controller --namespace ingress-nginx
