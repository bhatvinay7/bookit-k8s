#!/bin/bash
# deploy_local_docker.sh
# This script spins up a local Kubernetes cluster using 'kind' (Kubernetes IN Docker),
# installs the necessary prerequisites (cert-manager, operators), and then deploys
# the stateful-services Helm chart to test it locally.

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info() { echo -e "${YELLOW}==>${NC} $1"; }
pass() { echo -e "${GREEN}✔ PASS:${NC} $1"; }
fail() { echo -e "${RED}✘ FAIL:${NC} $1"; exit 1; }

# 1. Check prerequisites
command -v docker >/dev/null 2>&1 || fail "Docker is required but not installed."
command -v kind >/dev/null 2>&1 || fail "Kind (Kubernetes IN Docker) is required but not installed."
command -v helm >/dev/null 2>&1 || fail "Helm is required but not installed."
command -v kubectl >/dev/null 2>&1 || fail "kubectl is required but not installed."

CLUSTER_NAME="bookit-local-test"

# 2. Create Kind Cluster
info "Creating local Kubernetes cluster in Docker (kind)..."
if kind get clusters | grep -q "^$CLUSTER_NAME$"; then
    info "Cluster '$CLUSTER_NAME' already exists. Skipping creation."
else
    kind create cluster --name "$CLUSTER_NAME"
    pass "Kind cluster created."
fi

kubectl cluster-info --context kind-$CLUSTER_NAME

# 3. Install Cert-Manager (Required for RabbitMQ Operator and TLS)
info "Installing cert-manager..."
helm repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
helm repo update jetstack >/dev/null 2>&1
helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --set crds.enabled=true \
  --wait

pass "cert-manager installed."

# 4. Install RabbitMQ Cluster Operator
info "Installing RabbitMQ Cluster Operator..."
kubectl apply -f https://github.com/rabbitmq/cluster-operator/releases/latest/download/cluster-operator.yml
info "Waiting for RabbitMQ Operator to be ready..."
kubectl -n rabbitmq-system wait --for=condition=Available deployment/rabbitmq-cluster-operator --timeout=120s
pass "RabbitMQ Operator installed."

# 5. Install Prometheus Operator CRDs (Required for ServiceMonitors to render)
info "Installing Prometheus Operator CRDs (so ServiceMonitor resources don't fail)..."
kubectl apply --server-side -f https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/main/example/prometheus-operator-crd/monitoring.coreos.com_servicemonitors.yaml
pass "Prometheus CRDs installed."

# 6. Deploy Stateful-Services Helm Chart
info "Deploying stateful-services Helm chart..."
kubectl create namespace bookit --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install stateful-services ./charts/stateful-services \
  --namespace bookit \
  -f ./charts/stateful-services/values-development.yaml \
  --set postgres.useCloudProvider=false \
  --set mongodb.useCloudProvider=false \
  --set redis.useCloudProvider=false \
  --set rabbitmq.useCloudProvider=false \
  --wait \
  --timeout 5m

pass "stateful-services Helm chart deployed successfully!"

info "You can now check the running pods with:"
echo "kubectl get pods -n bookit"
echo "kubectl get rabbitmqclusters -n bookit"
echo ""
info "To clean up and destroy the local cluster, run:"
echo "kind delete cluster --name $CLUSTER_NAME"
