#!/usr/bin/env python3
"""Render deployments and check monitoring dependencies, namespaces and image refs.

Normal validation accepts documented production digest sentinels. Bootstrap and
promotion must pass --require-images prod to reject incomplete releases.
"""

import argparse
import json
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


def check_observability(apps, infra, values):
    """Check contracts across the independently deployed telemetry components."""
    def resource(kind, name):
        return next(d for d in apps + infra
                    if d["kind"] == kind and d["metadata"]["name"] == name)

    sources = {d["uid"]: d for d in values["grafana"]["additionalDataSources"]}
    tempo, loki = sources["tempo"], sources["loki"]
    check(tempo["url"] == "http://tempo.monitoring.svc.cluster.local:3200",
          "Grafana must query Tempo's HTTP API, not its OTLP receiver")
    check(loki["url"] == "http://loki-stack.monitoring.svc.cluster.local:3100",
          "Loki datasource must resolve to the Loki Helm Service")
    link = tempo["jsonData"]["tracesToLogsV2"]
    check(link["datasourceUid"] == loki["uid"] and link["customQuery"]
          and '{namespace="bookit"}' in link["query"]
          and "$${__span.traceId}" in link["query"],
          "Trace-to-log link must use the ingested labels and an escaped trace ID variable")
    derived = loki["jsonData"]["derivedFields"][0]
    trace_id = "a" * 32
    # Fluent Bit preserves both compact/pretty JSON and nested merged events.
    for line in [json.dumps({"trace_id": trace_id}),
                 json.dumps({"event": {"trace_id": trace_id}}, separators=(",", ":"))]:
        match = re.search(derived["matcherRegex"], line)
        check(match and match.group(1) == trace_id and derived["datasourceUid"] == tempo["uid"],
              "Loki derived field must resolve JSON trace IDs back to Tempo")

    alertmanager = values["alertmanager"]
    smtp = alertmanager["config"]["global"]
    check(
        "smtp_auth_password" not in smtp
        and smtp.get("smtp_auth_password_file")
        == "/etc/alertmanager/secrets/alertmanager-secret/smtp_auth_password"
        and "alertmanager-secret" in alertmanager["alertmanagerSpec"].get("secrets", []),
        "Alertmanager must mount the sealed SMTP credential instead of storing it in Helm values",
    )

    collector = yaml.safe_load(resource("ConfigMap", "otel-collector-config")["data"]["config.yaml"])
    connector = collector["connectors"]["spanmetrics"]
    check(connector["namespace"] == "traces.spanmetrics" and connector["histogram"]["unit"] == "s",
          "Spanmetric namespace/units must agree with Grafana queries")
    check(
        connector.get("metrics_flush_interval") == "15s"
        and {d.get("name") for d in connector.get("dimensions", [])} >= {"k8s.pod.name"},
        "Spanmetrics must expose bounded per-pod data at a load-test-friendly interval",
    )
    queries = str(tempo["jsonData"]["tracesToMetrics"]["queries"])
    check("bookit_traces_spanmetrics_duration_seconds_bucket" in queries and "latency_bucket" not in queries,
          "Grafana must use the connector's duration histogram")
    check(collector["exporters"]["otlp/tempo"]["endpoint"] == "tempo.monitoring.svc.cluster.local:4317",
          "Collector must send traces to Tempo's gRPC receiver")
    tailer = resource("ConfigMap", "fluent-bit-config")["data"]["fluent-bit.conf"]
    check(re.search(r"Name\s+loki\b", tailer)
          and "namespace=$kubernetes['namespace_name']" in tailer,
          "Fluent Bit must provide the Loki namespace label used in Grafana")
    loki_values = yaml.safe_load(resource("Application", "loki-stack")["spec"]["source"]["helm"]["values"])
    check(loki_values["loki"]["persistence"]["enabled"] and not loki_values["promtail"]["enabled"],
          "Loki must persist logs and use only one node tailer")
    check(not loki_values["grafana"]["sidecar"]["datasources"]["enabled"],
          "Loki chart must not provision a second datasource with a different UID")
    tempo_pod = resource("Deployment", "tempo")["spec"]["template"]["spec"]
    mounts = tempo_pod["containers"][0]["volumeMounts"]
    mount = next(m for m in mounts if m["mountPath"] == "/var/tempo")
    volume = next(v for v in tempo_pod["volumes"] if v["name"] == mount["name"])
    resource("PersistentVolumeClaim", volume["persistentVolumeClaim"]["claimName"])
    storage = yaml.safe_load(resource("ConfigMap", "tempo-config")["data"]["tempo.yaml"])["storage"]["trace"]
    check(all(storage[key]["path"].startswith(mount["mountPath"] + "/") for key in ["local", "wal"]),
          "Both Tempo blocks and WAL must reside on the PVC")
    web = resource("Deployment", "web")["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in web["env"]}
    check(env["OTEL_EXPORTER_OTLP_ENDPOINT"].endswith(":4318")
          and env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf",
          "Next.js exporter needs the collector HTTP endpoint")
    dashboard = resource("ConfigMap", "bookit-distributed-tracing-dashboard")
    for content in dashboard["data"].values():
        json.loads(content)
    load_test_dashboard = resource("ConfigMap", "bookit-load-test-dashboard")
    check(
        load_test_dashboard["metadata"].get("annotations", {}).get("grafana_folder")
        == "testLoad",
        "Load-test dashboard must be provisioned in Grafana's testLoad folder",
    )
    for content in load_test_dashboard["data"].values():
        parsed = json.loads(content)
        check(parsed.get("uid") == "bookit-load-test", "Load-test dashboard UID is incorrect")
        check(
            "bookit_load_test_stage_requested_requests" in content
            and "bookit_load_test_stage_target_rps" in content
            and "bookit_load_test_rust_lock_requested_requests" in content
            and "k8s_pod_name" in content,
            "Load-test dashboard must expose staged load and per-pod Gateway Keeper metrics",
        )
    check(
        values["grafana"]["sidecar"]["dashboards"].get("folderAnnotation")
        == "grafana_folder",
        "Grafana dashboard sidecar must honor the testLoad folder annotation",
    )


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
        for d in apps + infra:
            if d["kind"] in CLUSTER_SCOPED:
                check(
                    not d["metadata"].get("namespace"),
                    f"{d['kind']}/{d['metadata']['name']} must not have a namespace",
                )
        services = {
            (namespace(d, app_namespace), d["metadata"]["name"]): d
            for d in apps if d["kind"] == "Service"
        }
        for d in apps:
            if d["kind"] not in CLUSTER_SCOPED:
                check(
                    namespace(d, app_namespace) == app_namespace,
                    f"{apps_path}: {d['kind']}/{d['metadata']['name']} is in the wrong namespace",
                )
            if d["kind"] == "Ingress":
                for rule in d["spec"].get("rules", []):
                    for path in rule.get("http", {}).get("paths", []):
                        backend = path["backend"].get("service")
                        if not backend:
                            continue
                        key = (namespace(d, app_namespace), backend["name"])
                        check(key in services, f"Ingress backend Service {key} is missing")
                        port = backend["port"]
                        check(
                            any(
                                ("name" in port and p.get("name") == port["name"])
                                or ("number" in port and p["port"] == port["number"])
                                for p in services[key]["spec"]["ports"]
                            ),
                            f"Ingress backend Service {key} has no matching port {port}",
                        )
        expected_hpa_targets = {
            "http-server",
            "gateway-keeper",
            "ws-server",
            "web",
            "search-server",
            "lock-server",
            "payment-processor",
            "outbox-server",
            "notification-worker",
        }
        deployments = {
            d["metadata"]["name"]: d
            for d in apps
            if d["kind"] == "Deployment"
        }
        hpas = {
            d["metadata"]["name"]: d
            for d in apps
            if d["kind"] == "HorizontalPodAutoscaler"
        }
        check(
            expected_hpa_targets == set(hpas),
            "Every scalable Bookit workload must have exactly one HPA",
        )
        for name, hpa in hpas.items():
            target = hpa["spec"]["scaleTargetRef"]
            check(
                target == {"apiVersion": "apps/v1", "kind": "Deployment", "name": name},
                f"HPA {name} must target its matching Deployment",
            )
            check(
                hpa["spec"]["minReplicas"] == 1 and hpa["spec"]["maxReplicas"] > 1,
                f"HPA {name} must preserve the one-replica baseline and be able to scale out",
            )
            resources = deployments[name]["spec"]["template"]["spec"]["containers"][0]["resources"]
            check(
                resources.get("requests", {}).get("cpu") and resources.get("requests", {}).get("memory"),
                f"HPA {name} requires CPU and memory requests on its target",
            )
            metric_resources = {m["resource"]["name"] for m in hpa["spec"]["metrics"]}
            check(metric_resources == {"cpu", "memory"}, f"HPA {name} must use CPU and memory metrics")
        gateway_service = next(
            d for d in apps
            if d["kind"] == "Service" and d["metadata"]["name"] == "gateway-keeper"
        )
        check(
            gateway_service["spec"].get("clusterIP") != "None",
            "Gateway Keeper must use a ClusterIP Service to balance traffic across HPA replicas",
        )
        runner = next(
            d for d in apps
            if d["kind"] == "CronJob" and d["metadata"]["name"] == "bookit-rust-lock-load"
        )
        runner_spec = runner["spec"]
        runner_container = runner_spec["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        check(
            runner_spec.get("suspend") is True
            and runner_spec.get("concurrencyPolicy") == "Forbid"
            and runner_container.get("envFrom", [{}])[0].get("configMapRef", {}).get("name")
            == "manual-load-test-config",
            "Rust load generator must be a suspended, manually configured CronJob template",
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
        if project == "bookit":
            check_observability(apps, infra, values)
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
        if project == "bookit":
            for values_file in [
                ROOT / "charts/stateful-services/values-development.yaml",
                ROOT / "charts/stateful-services/values-deployment1.yaml",
                ROOT / "charts/stateful-services/values-production.yaml",
            ]:
                redis = yaml.safe_load(values_file.read_text())["redis"]
                check(
                    redis["replicas"] in {3, 5} and redis["proxyReplicas"] >= 2,
                    f"{values_file.name} must use 3 or 5 Redis members and two proxy replicas",
                )
            loadbalancer = render("loadbalancer")
            ingress_hpa = next(
                d for d in loadbalancer
                if d["kind"] == "HorizontalPodAutoscaler"
                and d["metadata"]["name"] == "ingress-nginx-controller"
            )
            check(
                ingress_hpa["metadata"].get("namespace") == "ingress-nginx"
                and ingress_hpa["spec"]["scaleTargetRef"]["name"] == "ingress-nginx-controller"
                and ingress_hpa["spec"]["minReplicas"] == 1
                and ingress_hpa["spec"]["maxReplicas"] > 1,
                "Ingress controller must have a scalable one-replica baseline",
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
