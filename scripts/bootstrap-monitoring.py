#!/usr/bin/env python3
"""Bootstrap missing Prometheus CRDs and the Argo-managed operator before apps.

Run from the GitOps repository root with an active kubectl context. Requires
Helm, kubectl and PyYAML. The argument is the environment's infra Kustomize path.
Existing CRD schemas are preserved; upgrades remain owned by the chart's Argo app.
"""

import json
import os
import subprocess
import sys

import yaml

KUBECTL = os.environ.get("KUBECTL", "kubectl")


class KubernetesLoader(yaml.SafeLoader):
    pass


# CRD schemas contain a literal '=' enum value; YAML 1.1's value tag has no
# default SafeLoader constructor. Kubernetes treats this scalar as a string.
KubernetesLoader.add_constructor(
    "tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node)
)


def run(*args, data=None):
    return subprocess.check_output(args, input=data, text=True)


def main():
    if len(sys.argv) != 2:
        raise SystemExit(
            "Usage: python3 scripts/bootstrap-monitoring.py infra/overlays/dev"
        )
    resources = [
        d for d in yaml.safe_load_all(run(KUBECTL, "kustomize", sys.argv[1])) if d
    ]
    app = next(
        d
        for d in resources
        if d["kind"] == "Application"
        and d["spec"].get("source", {}).get("chart") == "kube-prometheus-stack"
    )
    source = app["spec"]["source"]
    crds = [
        d
        for d in yaml.load_all(
            run(
                "helm",
                "show",
                "crds",
                source["chart"],
                "--repo",
                source["repoURL"],
                "--version",
                source["targetRevision"],
            ),
            Loader=KubernetesLoader,
        )
        if d
    ]
    required = {
        "servicemonitors.monitoring.coreos.com",
        "prometheusrules.monitoring.coreos.com",
        "prometheuses.monitoring.coreos.com",
    }
    if not crds or any(d["kind"] != "CustomResourceDefinition" for d in crds):
        raise SystemExit("Pinned monitoring chart did not return CRDs")
    if not required <= {d["metadata"]["name"] for d in crds}:
        raise SystemExit("Pinned monitoring chart is missing required CRDs")
    installed = {
        d["metadata"]["name"]
        for d in json.loads(run(KUBECTL, "get", "crds", "-o", "json"))["items"]
    }
    missing = [d for d in crds if d["metadata"]["name"] not in installed]
    if missing:
        print(
            run(
                KUBECTL,
                "apply",
                "--server-side",
                "-f",
                "-",
                data=yaml.safe_dump_all(missing),
            )
        )
    for crd in crds:
        print(
            run(
                KUBECTL,
                "wait",
                "--for=condition=Established",
                "crd/" + crd["metadata"]["name"],
                "--timeout=5m",
            )
        )

    # Merge only this health customization; preserve other argocd-cm settings.
    health = """local hs = {status = "Progressing", message = "Waiting for child application sync"}
if obj.metadata.annotations ~= nil and obj.metadata.annotations["argocd.argoproj.io/ignore-healthcheck"] == "true" then
  return {status = "Healthy", message = "Child health explicitly excluded"}
end
if obj.status ~= nil then
  local health = obj.status.health
  if health ~= nil and health.status == "Degraded" then return health end
  if obj.status.sync ~= nil and obj.status.sync.status == "Synced" and health ~= nil then
    return health
  end
end
return hs
"""
    key = "resource.customizations.health.argoproj.io_Application"
    config = json.loads(
        run(KUBECTL, "get", "configmap", "argocd-cm", "-n", "argocd", "-o", "json")
    )
    if not config.get("data", {}).get(key):
        print(
            run(
                KUBECTL,
                "patch",
                "configmap",
                "argocd-cm",
                "-n",
                "argocd",
                "--type=merge",
                "-p",
                json.dumps({"data": {key: health}}),
            )
        )
    print(run(KUBECTL, "apply", "--server-side", "-f", "-", data=yaml.safe_dump(app)))
    namespace = app["spec"]["destination"]["namespace"]
    release = source.get("helm", {}).get("releaseName", app["metadata"]["name"])
    # Wait for creation as well as rollout on an empty cluster.
    selector = f"app=kube-prometheus-stack-operator,release={release}"
    print(
        run(
            KUBECTL,
            "wait",
            "--for=create",
            "deployment",
            "-n",
            namespace,
            "-l",
            selector,
            "--timeout=10m",
        )
    )
    print(
        run(
            KUBECTL,
            "rollout",
            "status",
            "deployment",
            "-n",
            namespace,
            "-l",
            selector,
            "--timeout=10m",
        )
    )
    print("Monitoring CRDs are established and the Prometheus operator is ready.")


if __name__ == "__main__":
    main()
