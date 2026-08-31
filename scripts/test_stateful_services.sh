#!/usr/bin/env bash
# =============================================================================
# test_stateful_services.sh
# Automated validation for the stateful-services Helm chart and Kustomize
# overlays. Run this locally before pushing changes to catch YAML/template
# errors before Argo CD tries to sync them.
#
# Requirements: helm, kustomize, kubectl (for --dry-run), yq (optional)
# Usage: ./scripts/test_stateful_services.sh
# =============================================================================

set -uo pipefail

CHART_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/charts/stateful-services"
INFRA_DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/infra/overlays/dev"
APPS_DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/apps/overlays/dev"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0

pass() { echo -e "${GREEN}✔ PASS${NC}: $1"; ((PASS++)); }
fail() { echo -e "${RED}✘ FAIL${NC}: $1"; ((FAIL++)); }
info() { echo -e "${YELLOW}→${NC} $1"; }

# -----------------------------------------------------------------------------
echo ""
echo "==============================="
echo " stateful-services Chart Tests"
echo "==============================="

# 1. Helm lint
info "Running helm lint on stateful-services chart..."
LINT_OUT=$(helm lint "$CHART_DIR" -f "$CHART_DIR/values-development.yaml" \
     --set redis.useCloudProvider=false \
     --set postgres.useCloudProvider=true \
     --set mongodb.useCloudProvider=true 2>&1)
LINT_EXIT=$?
if [ $LINT_EXIT -eq 0 ]; then
  pass "helm lint (development values)"
else
  fail "helm lint (development values)"
  echo "$LINT_OUT"
fi

# 2. Helm template renders without errors (offline - no cluster needed)
info "Rendering Helm templates (development, non-cloud rabbitmq+redis)..."
RENDERED=$(helm template stateful-services "$CHART_DIR" \
  -f "$CHART_DIR/values-development.yaml" \
  --set redis.useCloudProvider=false \
  --set postgres.useCloudProvider=true \
  --set mongodb.useCloudProvider=true 2>&1)

if echo "$RENDERED" | grep -q "Error:"; then
  fail "helm template rendered with errors"
  echo "$RENDERED"
else
  pass "helm template render (development)"
fi


# 8. Verify monitoring-stack.yaml (chess legacy) is deleted
info "Verifying legacy monitoring-stack.yaml has been deleted..."
LEGACY_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/infra/base/monitoring/monitoring-stack.yaml"
if [ -f "$LEGACY_FILE" ]; then
  fail "Legacy monitoring-stack.yaml still exists – delete it!"
else
  pass "Legacy monitoring-stack.yaml successfully deleted"
fi

# -----------------------------------------------------------------------------
echo ""
echo "============================="
echo " Kustomize Build Tests"
echo "============================="

# 9. Infra dev overlay builds without errors
info "Building infra/overlays/dev kustomize..."
if kubectl kustomize "$INFRA_DEV_DIR" > /dev/null 2>&1; then
  pass "kustomize build infra/overlays/dev"
else
  fail "kustomize build infra/overlays/dev"
  kubectl kustomize "$INFRA_DEV_DIR" 2>&1 | tail -20
fi

# 10. Apps dev overlay builds without errors
info "Building apps/overlays/dev kustomize..."
if kubectl kustomize "$APPS_DEV_DIR" > /dev/null 2>&1; then
  pass "kustomize build apps/overlays/dev"
else
  fail "kustomize build apps/overlays/dev"
  kubectl kustomize "$APPS_DEV_DIR" 2>&1 | tail -20
fi

# 11. KEDA workers - check no conflicting port 5671 vs 5672 issues
info "Checking KEDA worker AMQP connection strings..."
KEDA_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/apps/overlays/dev/keda-workers.yaml"
if [ -f "$KEDA_FILE" ]; then
  AMQP_HOST=$(grep "host:" "$KEDA_FILE" | head -1)
  if echo "$AMQP_HOST" | grep -q "amqps://"; then
    pass "KEDA workers use amqps:// (TLS AMQP)"
  else
    fail "KEDA workers NOT using amqps:// - check connection string"
  fi
  if echo "$AMQP_HOST" | grep -q ":5671"; then
    pass "KEDA workers target port 5671 (AMQPS)"
  else
    fail "KEDA workers NOT using port 5671 - potential port mismatch"
  fi
else
  info "keda-workers.yaml not found at expected path, skipping"
fi

# -----------------------------------------------------------------------------
echo ""
echo "=============================="
echo " Chart Version Check"
echo "=============================="

CHART_VERSION=$(grep "^version:" "$CHART_DIR/Chart.yaml" | awk '{print $2}')
info "Current chart version: $CHART_VERSION"
if [[ "$CHART_VERSION" == "1.0.3" ]]; then
  pass "Chart version is 1.0.1 (latest)"
else
  fail "Chart version is $CHART_VERSION - expected 1.0.3"
fi

# -----------------------------------------------------------------------------
echo ""
echo "=============================="
echo " Summary"
echo "=============================="
echo -e "  ${GREEN}Passed${NC}: $PASS"
echo -e "  ${RED}Failed${NC}: $FAIL"
echo ""

if [ "$FAIL" -gt 0 ]; then
  echo -e "${RED}Some tests failed. Fix them before pushing.${NC}"
  exit 1
else
  echo -e "${GREEN}All tests passed! Safe to push.${NC}"
  exit 0
fi
