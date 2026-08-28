#!/bin/bash
set -e

echo "Installing NGINX Ingress Controller..."

# Add the official ingress-nginx Helm repository
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update

# Install the NGINX Ingress Controller
# This explicitly creates a Layer 4 LoadBalancer (TCP pass-through for ports 80 and 443)
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --set controller.service.type=LoadBalancer \
  --set controller.ingressClassResource.default=true

# -----------------------------------------------------------------------------
# AWS NOTE: If you are running on AWS (EKS) and want to force a Network Load Balancer (NLB),
# you would uncomment the following lines and add them to the helm command above:
# 
#  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"="nlb"
# -----------------------------------------------------------------------------

echo "Waiting for the LoadBalancer IP to be assigned..."
echo "Run: kubectl get service ingress-nginx-controller --namespace ingress-nginx -w"
