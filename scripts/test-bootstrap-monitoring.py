#!/usr/bin/env python3
"""Exercise bootstrap ordering and missing-CRD recovery without a cluster."""

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

spec = importlib.util.spec_from_file_location(
    "bootstrap", Path(__file__).with_name("bootstrap-monitoring.py")
)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
NAMES = [
    "servicemonitors.monitoring.coreos.com",
    "prometheusrules.monitoring.coreos.com",
    "prometheuses.monitoring.coreos.com",
]
CRDS = [{"kind": "CustomResourceDefinition", "metadata": {"name": n}} for n in NAMES]
APP = {
    "kind": "Application",
    "metadata": {"name": "kube-prometheus-stack"},
    "spec": {
        "source": {
            "chart": "kube-prometheus-stack",
            "repoURL": "https://example.invalid",
            "targetRevision": "51.2.0",
        },
        "destination": {"namespace": "monitoring"},
    },
}


class BootstrapTest(unittest.TestCase):
    def invoke(self, installed=(), fail_wait=False, chart=None, health=None):
        calls = []

        def run(*args, data=None):
            calls.append((args, data))
            if args[1] == "kustomize":
                return yaml.safe_dump(APP)
            if args[0] == "helm":
                return yaml.safe_dump_all(CRDS) if chart is None else chart
            if args[1:3] == ("get", "crds"):
                return json.dumps(
                    {"items": [{"metadata": {"name": n}} for n in installed]}
                )
            if args[1:3] == ("get", "configmap"):
                return json.dumps({"data": health or {}})
            if args[1] == "wait" and fail_wait:
                raise subprocess.CalledProcessError(1, args)
            return ""

        with (
            patch.object(bootstrap, "run", side_effect=run),
            patch.object(sys, "argv", ["bootstrap", "infra"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                bootstrap.main()
            finally:
                self.calls = calls
        return calls

    def test_only_missing_crds_are_applied_before_application(self):
        calls = self.invoke(installed=NAMES[:1])
        applies = [
            (i, list(yaml.safe_load_all(data)))
            for i, (args, data) in enumerate(calls)
            if args[1] == "apply"
        ]
        self.assertEqual([d["metadata"]["name"] for d in applies[0][1]], NAMES[1:])
        app_index = applies[1][0]
        waits = [
            i
            for i, (args, _) in enumerate(calls)
            if "wait" in args and "--for=condition=Established" in args
        ]
        self.assertEqual(len(waits), len(NAMES))
        self.assertTrue(all(applies[0][0] < i < app_index for i in waits))
        self.assertEqual(calls[-1][0][1:3], ("rollout", "status"))

    def test_existing_schemas_and_health_are_preserved(self):
        calls = self.invoke(
            installed=NAMES,
            health={
                "resource.customizations.health.argoproj.io_Application": "existing"
            },
        )
        self.assertEqual(sum(args[1] == "apply" for args, _ in calls), 1)
        self.assertFalse(any(args[1] == "patch" for args, _ in calls))

    def test_failed_registration_prevents_application_apply(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke(installed=NAMES, fail_wait=True)
        self.assertFalse(any(args[1] == "apply" for args, _ in self.calls))

    def test_empty_chart_stops_before_any_cluster_mutation(self):
        with self.assertRaises(SystemExit):
            self.invoke(chart="")
        self.assertFalse(any(args[1] in {"apply", "patch"} for args, _ in self.calls))

    def test_crd_enum_equals_is_a_string(self):
        self.assertEqual(
            yaml.load("enum: [=, '!=']", Loader=bootstrap.KubernetesLoader),
            {"enum": ["=", "!="]},
        )


if __name__ == "__main__":
    unittest.main()
