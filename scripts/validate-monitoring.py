#!/usr/bin/env python3
"""Render deployments and check monitoring dependencies, namespaces and image refs.

Normal validation accepts documented production digest sentinels. Bootstrap and
promotion must pass --require-images prod to reject incomplete releases.
"""

import argparse
import os
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
KUBECTL = os.environ.get("KUBECTL", "kubectl")
CLUSTER_SCOPED = {
    "Namespace",
    "ClusterRole",
    "ClusterRoleBinding",
    "ClusterIssuer",
    "CustomResourceDefinition",
    "APIService",
    "IngressClass",
}


def check(condition, message):
    if not condition:
        raise ValueError(message)


def render(path):
    return [
        d
        for d in yaml.safe_load_all(
            subprocess.check_output([KUBECTL, "kustomize", str(ROOT / path)], text=True)
        )
        if d
    ]


def images(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "image" and isinstance(v, str):
                yield v
            else:
                yield from images(v)
    elif isinstance(value, list):
        for item in value:
            yield from images(item)


def wave(obj):
    return int(
        obj["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", "0")
    )


def namespace(obj, fallback):
    return obj["metadata"].get("namespace", fallback)


def matches(labels, selector):
    return all(labels.get(k) == v for k, v in selector.items())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-images", choices=["dev", "prod"])
    args = parser.parse_args()
    chess = (ROOT / "apps/metrics.yaml").exists()
    project = (
        "chess"
        if chess
        else ("auction" if (ROOT / "argocd/chart").exists() else "bookit")
    )
    app_namespace = "default" if chess else project
    targets = (
        [("dev", "apps", "infra")]
        if chess
        else [
            ("dev", "apps/overlays/dev", "infra/overlays/dev"),
            ("prod", "apps/overlays/prod", "infra/overlays/prod"),
        ]
    )
    for env, apps_path, infra_path in targets:
        apps, infra = render(apps_path), render(infra_path)
        for d in apps:
            if d["kind"] not in CLUSTER_SCOPED:
                check(
                    namespace(d, app_namespace) == app_namespace,
                    f"{apps_path}: {d['kind']}/{d['metadata']['name']} is in the wrong namespace",
                )
        for d in apps:
            if d["kind"] == "Deployment":
                for c in d["spec"]["template"]["spec"]["containers"]:
                    defined = set()
                    for entry in c.get("env", []):
                        if entry["name"] == "OTEL_RESOURCE_ATTRIBUTES":
                            check(
                                {"K8S_NAMESPACE", "POD_NAME"} <= defined,
                                "Downward API env vars must precede OTEL_RESOURCE_ATTRIBUTES",
                            )
                        defined.add(entry["name"])
            for ref in images(d):
                if d["kind"] == "Job" and ref == "postgres:16-alpine":
                    continue  # Auction's database migration tool is a third-party image.
                check(
                    ref.startswith(f"ghcr.io/bhatvinay7/{project}-"),
                    f"Unqualified/wrong registry image: {ref}",
                )
                if env == "prod":
                    check(
                        re.search(r"@sha256:[0-9a-f]{64}$", ref),
                        f"Production image must use a digest: {ref}",
                    )
                    if args.require_images == env:
                        check(
                            not ref.endswith("sha256:" + "0" * 64),
                            f"Promote a verified production digest before bootstrap: {ref}",
                        )
                else:
                    check(
                        re.search(r":commit-[0-9a-f]{7,40}$", ref),
                        f"Development image must use a commit tag: {ref}",
                    )
        stack = next(
            d
            for d in infra
            if d["kind"] == "Application"
            and d["spec"].get("source", {}).get("chart") == "kube-prometheus-stack"
        )
        helm = stack["spec"]["source"]["helm"]
        values = yaml.safe_load(helm["values"])
        check(
            helm.get("skipCrds") is False and values["crds"]["enabled"],
            "Prometheus CRDs must be included",
        )
        check(
            namespace(stack, "") == "argocd"
            and stack["spec"]["destination"]["namespace"] == "monitoring",
            "Monitoring Application must live in argocd and deploy into monitoring",
        )
        check(
            "ServerSideApply=true" in stack["spec"]["syncPolicy"]["syncOptions"],
            "Large CRDs need server-side apply",
        )
        spec = values["prometheus"]["prometheusSpec"]
        check(
            spec["serviceMonitorSelectorNilUsesHelmValues"] is False
            and spec["serviceMonitorSelector"] == {}
            and spec["serviceMonitorNamespaceSelector"] == {},
            "Prometheus must discover the application/collector monitors across namespaces",
        )
        services = [d for d in apps + infra if d["kind"] == "Service"]
        monitor_targets = set()
        for d in [x for x in apps + infra if x["kind"] == "ServiceMonitor"]:
            if d in infra:
                check(
                    wave(stack) < wave(d),
                    f"Provider must precede {d['metadata']['name']}",
                )
            ns = namespace(d, app_namespace)
            selected_namespaces = (
                d["spec"].get("namespaceSelector", {}).get("matchNames", [ns])
            )
            labels = d["spec"]["selector"].get("matchLabels", {})
            check(
                labels, f"Use an explicit service selector for {d['metadata']['name']}"
            )
            selected = [
                s
                for s in services
                if namespace(s, app_namespace) in selected_namespaces
                and matches(s["metadata"].get("labels", {}), labels)
            ]
            check(selected, f"Monitor {d['metadata']['name']} selects no services")
            for s in selected:
                ports = {p["name"] for p in s["spec"]["ports"] if "name" in p}
                for endpoint in d["spec"]["endpoints"]:
                    check(
                        endpoint["port"] in ports,
                        f"{d['metadata']['name']} port {endpoint['port']} missing on {s['metadata']['name']}",
                    )
                    key = (
                        namespace(s, app_namespace),
                        s["metadata"]["name"],
                        endpoint["port"],
                        endpoint.get("path", "/metrics"),
                    )
                    check(key not in monitor_targets, f"Duplicate scrape target: {key}")
                    monitor_targets.add(key)
        for d in apps:
            if d["kind"] == "PrometheusRule":
                check(
                    d["metadata"]["labels"]["release"] == helm["releaseName"],
                    "PrometheusRule release selector mismatch",
                )
        # Service selectors and named target ports must resolve within a namespace.
        pods = [
            d
            for d in apps + infra
            if d["kind"] in {"Deployment", "StatefulSet", "DaemonSet"}
        ]
        for s in services:
            selector = s["spec"].get("selector")
            if not selector:
                continue
            selected = [
                p
                for p in pods
                if namespace(p, app_namespace) == namespace(s, app_namespace)
                and matches(
                    p["spec"]["template"]["metadata"].get("labels", {}), selector
                )
            ]
            # Some infra Services target workloads installed by a child Helm app.
            if s not in apps and not selected:
                continue
            check(selected, f"Service {s['metadata']['name']} selects no workload")
            for p in selected:
                named_ports = {
                    port["name"]
                    for c in p["spec"]["template"]["spec"]["containers"]
                    for port in c.get("ports", [])
                    if "name" in port
                }
                for port in s["spec"]["ports"]:
                    target = port.get("targetPort", port["port"])
                    if isinstance(target, str):
                        check(
                            target in named_ports,
                            f"{s['metadata']['name']} targetPort {target} is missing",
                        )
        print(
            f"{project} {env}: namespace, image, service port, monitor selector and CRD ordering checks passed"
        )
    if project == "bookit":
        for env in ["dev", "prod"]:
            appsets = list(
                yaml.safe_load_all((ROOT / f"argocd/{env}-apps.yaml").read_text())
            )
            for appset in appsets:
                elements = appset["spec"]["generators"][0]["list"]["elements"]
                check(
                    len(elements) == 1,
                    "Each ApplicationSet must target only the single local cluster",
                )
                check(
                    elements[0]["url"] == "https://kubernetes.default.svc",
                    "Remote cluster destination is not configured",
                )
                template = appset["spec"]["template"]["spec"]
                check(
                    template["destination"]
                    == {"server": "{{url}}", "namespace": "bookit"},
                    "Incorrect Bookit destination",
                )
                if "appsPath" in elements[0]:
                    check(
                        elements[0]["appsPath"] == f"apps/regions/{env}/us-east",
                        "Incorrect single-cluster overlay",
                    )
                    render(elements[0]["appsPath"])
        print(
            "bookit: single-cluster ApplicationSet destinations and active overlays passed"
        )


if __name__ == "__main__":
    main()
