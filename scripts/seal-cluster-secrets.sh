#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <dev|prod> <region> <kube-context>" >&2
  exit 2
}

[[ $# -eq 3 ]] || usage
environment="$1"
region="$2"
kube_context="$3"
[[ "$environment" == "dev" || "$environment" == "prod" ]] || usage

command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 1; }
command -v kubeseal >/dev/null || { echo "kubeseal is required" >&2; exit 1; }

required=(
  DATABASE_URL REPLICATION_URL MONGODB_URL REDIS_URL RABBITMQ_URL
  MONGODB_DB ELASTICSEARCH_URL GRPC_SERVER_URL HTTP_SERVER_URL
  SEARCH_SERVER_GRPC_URL JWT_SECRET INVITATION_ENCRYPTION_KEY
  RAZORPAY_KEY_ID RAZORPAY_KEY_SECRET SLOT_NAME PUBLICATION
  SMTP_HOST SMTP_PORT SMTP_SECURE SMTP_USER SMTP_PASS SMTP_FROM
  GMAIL_USER GMAIL_APP_PASSWORD SUPPORT_EMAIL
  CLOUDFLARE_R2_ACCOUNT_ID CLOUDFLARE_R2_ACCESS_KEY_ID
  CLOUDFLARE_R2_SECRET_ACCESS_KEY CLOUDFLARE_R2_ENDPOINT CLOUDFLARE_R2_BUCKET
  CLOUDFLARE_R2_PUBLIC_URL GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET
  NEXT_PUBLIC_API_URL NEXT_PUBLIC_SOCKET_URL NEXT_PUBLIC_WS_BASE_URL
  NEXT_PUBLIC_SEARCH_API_URL NEXT_PUBLIC_RAZORPAY_KEY_ID
  NEXT_PUBLIC_USE_REMOTE_STOCKFISH GHCR_USERNAME GHCR_TOKEN
)

for name in "${required[@]}"; do
  [[ -n "${!name:-}" ]] || { echo "required environment variable $name is empty" >&2; exit 1; }
done

validate_url() {
  local name="$1"
  local allowed="$2"
  local value="${!name}"
  [[ "$value" =~ ^(${allowed})://[^[:space:]]+$ ]] || {
    echo "$name must use one of these URL schemes: ${allowed//|/, }" >&2
    exit 1
  }
}

# Validate provider URLs without ever printing credential-bearing values.
validate_url DATABASE_URL 'postgres|postgresql'
validate_url REPLICATION_URL 'postgres|postgresql'
validate_url MONGODB_URL 'mongodb|mongodb\+srv'
validate_url REDIS_URL 'redis|rediss'
validate_url RABBITMQ_URL 'amqp|amqps'
validate_url CLOUDFLARE_R2_ENDPOINT 'https'

redis_cluster_url="${REDIS_CLUSTER_URL:-$REDIS_URL}"
redis_cluster_urls="${REDIS_CLUSTER_URLS:-$redis_cluster_url}"
redis_remote_url="${REDIS_REMOTE_URL:-$REDIS_URL}"

out_dir="apps/regions/${environment}/${region}/secrets"
[[ -d "$out_dir" ]] || { echo "unknown environment/region: ${environment}/${region}" >&2; exit 1; }

seal() {
  local secret_name="$1"
  local output="$2"
  shift 2
  kubectl --context "$kube_context" -n bookit create secret generic "$secret_name" \
    "$@" --dry-run=client -o yaml |
    kubeseal --context "$kube_context" --controller-name sealed-secrets-controller \
      --controller-namespace kube-system --format yaml > "${out_dir}/${output}"
}

seal backend-secrets sealed-backend-secrets.yaml \
  --from-literal=BOOKIT_ENVIRONMENT="$environment" \
  --from-literal=BOOKIT_REGION="$region" \
  --from-literal=DATABASE_URL="$DATABASE_URL" \
  --from-literal=REPLICATION_URL="$REPLICATION_URL" \
  --from-literal=MONGODB_URL="$MONGODB_URL" \
  --from-literal=MONGO_DB_URL="$MONGODB_URL" \
  --from-literal=MONGODB_DB="$MONGODB_DB" \
  --from-literal=REDIS_URL="$REDIS_URL" \
  --from-literal=REDIS_CLUSTER_URL="$redis_cluster_url" \
  --from-literal=REDIS_CLUSTER_URLS="$redis_cluster_urls" \
  --from-literal=REDIS_REMOTE_URL="$redis_remote_url" \
  --from-literal=RABBITMQ_URL="$RABBITMQ_URL" \
  --from-literal=ELASTICSEARCH_URL="$ELASTICSEARCH_URL" \
  --from-literal=GRPC_SERVER_URL="$GRPC_SERVER_URL" \
  --from-literal=HTTP_SERVER_URL="$HTTP_SERVER_URL" \
  --from-literal=SEARCH_SERVER_GRPC_URL="$SEARCH_SERVER_GRPC_URL" \
  --from-literal=JWT_SECRET="$JWT_SECRET" \
  --from-literal=INVITATION_ENCRYPTION_KEY="$INVITATION_ENCRYPTION_KEY" \
  --from-literal=RAZORPAY_KEY_ID="$RAZORPAY_KEY_ID" \
  --from-literal=RAZORPAY_KEY_SECRET="$RAZORPAY_KEY_SECRET" \
  --from-literal=SLOT_NAME="$SLOT_NAME" \
  --from-literal=PUBLICATION="$PUBLICATION" \
  --from-literal=SMTP_HOST="$SMTP_HOST" --from-literal=SMTP_PORT="$SMTP_PORT" \
  --from-literal=SMTP_SECURE="$SMTP_SECURE" --from-literal=SMTP_USER="$SMTP_USER" \
  --from-literal=SMTP_PASS="$SMTP_PASS" --from-literal=SMTP_FROM="$SMTP_FROM" \
  --from-literal=GMAIL_USER="$GMAIL_USER" \
  --from-literal=GMAIL_APP_PASSWORD="$GMAIL_APP_PASSWORD" \
  --from-literal=SUPPORT_EMAIL="$SUPPORT_EMAIL" \
  --from-literal=CLOUDFLARE_R2_ACCOUNT_ID="$CLOUDFLARE_R2_ACCOUNT_ID" \
  --from-literal=CLOUDFLARE_R2_ACCESS_KEY_ID="$CLOUDFLARE_R2_ACCESS_KEY_ID" \
  --from-literal=CLOUDFLARE_R2_SECRET_ACCESS_KEY="$CLOUDFLARE_R2_SECRET_ACCESS_KEY" \
  --from-literal=CLOUDFLARE_R2_ENDPOINT="$CLOUDFLARE_R2_ENDPOINT" \
  --from-literal=CLOUDFLARE_R2_BUCKET="$CLOUDFLARE_R2_BUCKET" \
  --from-literal=CLOUDFLARE_R2_PUBLIC_URL="$CLOUDFLARE_R2_PUBLIC_URL" \
  --from-literal=GOOGLE_CLIENT_ID="$GOOGLE_CLIENT_ID" \
  --from-literal=GOOGLE_CLIENT_SECRET="$GOOGLE_CLIENT_SECRET"

seal frontend-secrets sealed-frontend-secrets.yaml \
  --from-literal=BOOKIT_ENVIRONMENT="$environment" \
  --from-literal=BOOKIT_REGION="$region" \
  --from-literal=NEXT_PUBLIC_API_URL="$NEXT_PUBLIC_API_URL" \
  --from-literal=NEXT_PUBLIC_SOCKET_URL="$NEXT_PUBLIC_SOCKET_URL" \
  --from-literal=NEXT_PUBLIC_WS_BASE_URL="$NEXT_PUBLIC_WS_BASE_URL" \
  --from-literal=NEXT_PUBLIC_SEARCH_API_URL="$NEXT_PUBLIC_SEARCH_API_URL" \
  --from-literal=NEXT_PUBLIC_GOOGLE_CLIENT_ID="$GOOGLE_CLIENT_ID" \
  --from-literal=NEXT_PUBLIC_R2_PUBLIC_URL="$CLOUDFLARE_R2_PUBLIC_URL" \
  --from-literal=NEXT_PUBLIC_RAZORPAY_KEY_ID="$NEXT_PUBLIC_RAZORPAY_KEY_ID" \
  --from-literal=NEXT_PUBLIC_USE_REMOTE_STOCKFISH="$NEXT_PUBLIC_USE_REMOTE_STOCKFISH"

seal bookit-secrets sealed-platform-secrets.yaml \
  --from-literal=BOOKIT_ENVIRONMENT="$environment" \
  --from-literal=BOOKIT_REGION="$region" \
  --from-literal=CLOUDFLARE_R2_ACCOUNT_ID="$CLOUDFLARE_R2_ACCOUNT_ID" \
  --from-literal=CLOUDFLARE_R2_ACCESS_KEY_ID="$CLOUDFLARE_R2_ACCESS_KEY_ID" \
  --from-literal=CLOUDFLARE_R2_SECRET_ACCESS_KEY="$CLOUDFLARE_R2_SECRET_ACCESS_KEY" \
  --from-literal=CLOUDFLARE_R2_ENDPOINT="$CLOUDFLARE_R2_ENDPOINT" \
  --from-literal=CLOUDFLARE_R2_BUCKET="$CLOUDFLARE_R2_BUCKET" \
  --from-literal=CLOUDFLARE_R2_PUBLIC_URL="$CLOUDFLARE_R2_PUBLIC_URL" \
  --from-literal=AWS_ACCESS_KEY_ID="$CLOUDFLARE_R2_ACCESS_KEY_ID" \
  --from-literal=AWS_SECRET_ACCESS_KEY="$CLOUDFLARE_R2_SECRET_ACCESS_KEY" \
  --from-literal=AWS_ENDPOINT_URL="$CLOUDFLARE_R2_ENDPOINT" \
  --from-literal=AWS_DEFAULT_REGION="auto"

kubectl --context "$kube_context" -n bookit create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io --docker-username="$GHCR_USERNAME" \
  --docker-password="$GHCR_TOKEN" --dry-run=client -o yaml |
  kubeseal --context "$kube_context" --controller-name sealed-secrets-controller \
    --controller-namespace kube-system --format yaml > "${out_dir}/sealed-ghcr-secret.yaml"

printf '%s\n' \
  'apiVersion: kustomize.config.k8s.io/v1beta1' \
  'kind: Kustomization' \
  'resources:' \
  '- sealed-backend-secrets.yaml' \
  '- sealed-frontend-secrets.yaml' \
  '- sealed-platform-secrets.yaml' \
  '- sealed-ghcr-secret.yaml' > "${out_dir}/kustomization.yaml"

echo "sealed ${environment}/${region} secrets for context ${kube_context}"
