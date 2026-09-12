# Recover a monitoring sync failure

`ServiceMonitor` and `PrometheusRule` require the Prometheus Operator CRDs.
`SkipDryRunOnMissingResource` skips discovery errors during dry-run; it does
not install the CRDs. Application sync waves also need a child Application
health customization to wait for a Helm child to finish.

The application namespace is `bookit`. Monitoring resources live in `monitoring`;
the Helm Application object lives in `argocd`. These are separate concerns.

After publishing these GitOps changes, rerun the application repository's
Bootstrap ArgoCD workflow (select the matching environment). It validates image
references, installs only missing CRDs from the chart version in the rendered
infra manifest, waits for Established, registers the Prometheus Application and
waits for its operator deployment before registering workloads. Existing CRD
schemas and existing Argo child health configuration are preserved.

For an existing cluster, from this GitOps checkout with the correct kubectl
context and Helm, kubectl and PyYAML installed:

```sh
python3 scripts/bootstrap-monitoring.py infra/overlays/dev
kubectl get crd servicemonitors.monitoring.coreos.com prometheusrules.monitoring.coreos.com
kubectl get deployment -n monitoring -l app=kube-prometheus-stack-operator
```

For production overlays use `infra/overlays/prod` instead (chess uses `infra`).
Then refresh and sync the affected infra and workload applications in Argo CD.
A failed operation whose retries were exhausted needs a new Sync operation;
a refresh alone does not guarantee retry. If bootstrap fails, inspect the
`kube-prometheus-stack` Application events and operator deployment before syncing
consumers.

Validate changes locally:

```sh
python3 scripts/validate-monitoring.py
python3 scripts/test-bootstrap-monitoring.py
```

Before production bootstrap or promotion, use
`python3 scripts/validate-monitoring.py --require-images prod` for auction/bookit.
All-zero production digests are unpromoted placeholders, never deployable images.
Use the production promotion workflow to resolve verified GHCR digests; do not
replace them with guessed tags. Different development service commit tags are
valid when CI only builds changed services.

Bookit deploys to one cluster using `https://kubernetes.default.svc`. The
`us-east` directory and application names are retained as existing identifiers;
no eu-west cluster, secondary kubeconfig or region/context lists are required.
The CI runner uses the current context in the environment's `KUBECONFIG`.

References: [Argo sync options](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/),
[child application health](https://argo-cd.readthedocs.io/en/stable/operator-manual/health/#argocd-app).
