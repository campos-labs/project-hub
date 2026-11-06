"""Development runner for #102; local Kind only, fixed schema, synthetic events.

python spike_delivery_cases.py --output EXISTING_BOOTSTRAP_OUTPUT --mode direct|flux
Each mode requires a new result directory. Failed attempts are never replaced.
The existing verifier owns acceptance/conclusion/idempotency assertions.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import yaml

CLUSTER = "fulfillflow-spike-102"
TARGET = "notifications-worker"


def run(args, *, data=None, env=None, timeout=180):
    p = subprocess.run(
        list(map(str, args)),
        input=data,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    if p.returncode:
        raise RuntimeError(p.stderr[-2000:])
    return p.stdout


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def set_suspended(kube, value):
    """Exclusive lab controller: stop ongoing writers before direct mutations."""
    kube(
        "-n",
        "flux-system",
        "patch",
        "kustomization",
        "spike",
        "--type=merge",
        "-p",
        json.dumps({"spec": {"suspend": value}}),
    )
    kube(
        "-n",
        "flux-system",
        "scale",
        "deployment/kustomize-controller",
        "--replicas=" + ("0" if value else "1"),
    )
    if value:
        kube(
            "-n",
            "flux-system",
            "wait",
            "--for=delete",
            "pod",
            "-l",
            "app=kustomize-controller",
            "--timeout=120s",
        )
    else:
        kube(
            "-n",
            "flux-system",
            "rollout",
            "status",
            "deployment/kustomize-controller",
            "--timeout=120s",
        )


class Experiment:
    def __init__(self, root, mode, attempt):
        self.root, self.mode = root, mode
        if not re.fullmatch(r"[a-z0-9-]+", attempt):
            raise ValueError("Invalid attempt")
        self.attempt = attempt
        self.results = root / (mode + "-" + attempt + "-cases")
        self.results.mkdir(exist_ok=False)
        inputs = json.loads((root / "inputs.json").read_text())
        self.k = [
            str(Path(inputs["tools"]) / "kubectl/kubectl.exe"),
            "--kubeconfig",
            str(root / "private/kubeconfig"),
            "--context",
            "kind-" + CLUSTER,
            "--request-timeout=20s",
        ]
        identity = json.loads((root / "private/identity.json").read_text())
        ns = json.loads(self.kube("get", "namespace", "fulfillflow", "-o", "json"))
        if ns["metadata"]["uid"] != identity["namespace_uid"]:
            raise ValueError("Namespace identity changed")
        docs = yaml.safe_load_all((root / "bootstrap/runtime.yaml").read_text())
        self.base = next(
            x
            for x in docs
            if x and x["kind"] == "Deployment" and x["metadata"]["name"] == TARGET
        )
        self.base["metadata"]["namespace"] = "fulfillflow"
        self.gitroot = root / (mode + "-" + attempt + "-git")
        self.gitroot.mkdir()
        self.work = self.gitroot / "work"
        run(["git", "init", "--bare", self.gitroot / "desired.git"])
        run(["git", "init", "-b", "main", self.work])
        self.git("config", "user.name", "Delivery Spike")
        self.git("config", "user.email", "spike@example.invalid")
        self.git("remote", "add", "origin", str(self.gitroot / "desired.git"))
        self.rows = []

    def suspend(self, value):
        set_suspended(self.kube, value)

    def deliver(self, name, bad=False):
        name = self.mode + "-" + self.attempt + "-" + name
        sha, manifest = self.commit(name, bad)
        if self.mode == "direct":
            self.apply(manifest)
        else:
            self.reconcile(sha)
        self.wait_revision(name)
        return sha, name

    def record(self, row):
        self.rows.append(row)
        save(self.results / "results.json", self.rows)

    def remaining_cases(self):
        rejected, name = self.deliver("rejected", bad=True)
        # Confirm failure of the current ReplicaSet, not an older pod.
        until = time.monotonic() + 120
        while time.monotonic() < until:
            sets = json.loads(
                self.kube(
                    "-n",
                    "fulfillflow",
                    "get",
                    "rs",
                    "-l",
                    "app.kubernetes.io/name=" + TARGET,
                    "-o",
                    "json",
                )
            )["items"]
            owners = {
                x["metadata"]["uid"]
                for x in sets
                if x["spec"]["template"]["metadata"]
                .get("annotations", {})
                .get("spike/revision")
                == name
            }
            pods = json.loads(
                self.kube(
                    "-n",
                    "fulfillflow",
                    "get",
                    "pods",
                    "-l",
                    "app.kubernetes.io/name=" + TARGET,
                    "-o",
                    "json",
                )
            )["items"]
            failed = [
                p
                for p in pods
                if any(
                    o["uid"] in owners for o in p["metadata"].get("ownerReferences", [])
                )
                and any(
                    s.get("state", {}).get("waiting", {}).get("reason")
                    == "CrashLoopBackOff"
                    or s.get("state", {}).get("terminated", {}).get("exitCode", 0) != 0
                    for s in p.get("status", {}).get("containerStatuses", [])
                )
            ]
            if failed:
                save(self.results / "rejected-pods.json", failed)
                break
            time.sleep(2)
        else:
            raise TimeoutError("Expected startup failure not observed")
        recovered, healthy_name = self.deliver("recovered")
        functional = self.healthy("recovered")
        observed = []
        until = time.monotonic() + 130
        while time.monotonic() < until:
            obj = self.snapshot()
            revision = obj["spec"]["template"]["metadata"]["annotations"][
                "spike/revision"
            ]
            observed.append({"at": time.time(), "revision": revision})
            if revision != healthy_name:
                raise AssertionError("Rejected revision reapplied")
            time.sleep(5)
        save(self.results / "recovery-observation.json", observed)
        self.record(
            {
                "case": "startup_failure",
                "rejected": rejected,
                "recovered": recovered,
                "observation_seconds": 130,
                "functional": functional,
            }
        )

        # Delivery has happened; stop its owner before recording functional completion.
        sha, name = self.deliver("interrupted")
        before = self.snapshot()
        if self.mode == "flux":
            self.kube(
                "-n",
                "flux-system",
                "scale",
                "deployment/kustomize-controller",
                "--replicas=0",
            )
            self.kube(
                "-n",
                "flux-system",
                "wait",
                "--for=delete",
                "pod",
                "-l",
                "app=kustomize-controller",
                "--timeout=60s",
            )
            self.kube(
                "-n",
                "flux-system",
                "scale",
                "deployment/kustomize-controller",
                "--replicas=1",
            )
            self.kube(
                "-n",
                "flux-system",
                "rollout",
                "status",
                "deployment/kustomize-controller",
                "--timeout=120s",
            )
            self.reconcile(sha)
            mechanism = (
                "controller scaled to zero after apply; restarted before verification"
            )
        else:
            # A real child performs an idempotent apply, signals that checkpoint,
            # then waits without writing a completion journal. Terminate that child.
            checkpoint = self.results / "apply-checkpoint"
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import subprocess,sys,time; from pathlib import Path; subprocess.run(sys.argv[2:],check=True,stdout=subprocess.DEVNULL); Path(sys.argv[1]).write_text('applied'); time.sleep(120)",
                    str(checkpoint),
                    *self.k,
                    "apply",
                    "-f",
                    str(self.work / "deployment.json"),
                ]
            )
            try:
                until = time.monotonic() + 30
                while (
                    not checkpoint.exists()
                    and child.poll() is None
                    and time.monotonic() < until
                ):
                    time.sleep(0.2)
                if not checkpoint.exists():
                    raise RuntimeError("Child did not reach checkpoint")
            finally:
                child.terminate()
                child.wait(timeout=10)
            mechanism = (
                "local delivery child terminated after apply, before completion journal"
            )
        after = self.snapshot()
        if (
            before["metadata"]["uid"] != after["metadata"]["uid"]
            or before["spec"] != after["spec"]
        ):
            raise AssertionError("Unexpected deployment mutation during recovery")
        self.record(
            {
                "case": "delivery_interruption",
                "sha": sha,
                "mechanism": mechanism,
                "same_deployment_spec": True,
                "functional": self.healthy("interrupted"),
            }
        )

        # Approve an emergency template annotation, then adopt it into Git.
        if self.mode == "flux":
            self.suspend(True)
        emergency = self.mode + "-" + self.attempt + "-emergency"
        self.kube(
            "-n",
            "fulfillflow",
            "patch",
            "deployment",
            TARGET,
            "--type=merge",
            "-p",
            json.dumps(
                {
                    "spec": {
                        "template": {
                            "metadata": {"annotations": {"spike/revision": emergency}}
                        }
                    }
                }
            ),
        )
        self.wait_revision(emergency)
        sha, manifest = self.commit(emergency)
        if self.mode == "flux":
            self.suspend(False)
            self.reconcile(sha)
        else:
            self.apply(manifest)
        self.record(
            {
                "case": "emergency_change",
                "sha": sha,
                "policy": "suspend GitOps owner before authorized edit; adopt change in Git before resume",
                "functional": self.healthy("emergency"),
            }
        )

    def kube(self, *args, data=None):
        return run(self.k + list(args), data=data)

    def git(self, *args):
        return run(["git", "-C", self.work, *args])

    def commit(self, name, bad=False):
        manifest = copy.deepcopy(self.base)
        manifest["spec"]["template"]["metadata"].setdefault("annotations", {})[
            "spike/revision"
        ] = name
        if bad:
            for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]:
                if item["name"] == "DB_POOL_SIZE":
                    item["value"] = "0"
        save(self.work / "deployment.json", manifest)
        save(
            self.work / "kustomization.yaml",
            {
                "apiVersion": "kustomize.config.k8s.io/v1beta1",
                "kind": "Kustomization",
                "resources": ["deployment.json"],
            },
        )
        self.git("add", ".")
        self.git("commit", "-m", name)
        self.git("push", "origin", "main")
        return self.git("rev-parse", "HEAD").strip(), manifest

    def apply(self, manifest):
        self.kube("apply", "-f", "-", data=json.dumps(manifest))

    def snapshot(self):
        return json.loads(
            self.kube("-n", "fulfillflow", "get", "deployment", TARGET, "-o", "json")
        )

    def wait_revision(self, name):
        until = time.monotonic() + 120
        while time.monotonic() < until:
            obj = self.snapshot()
            if (
                obj["spec"]["template"]["metadata"]
                .get("annotations", {})
                .get("spike/revision")
                == name
            ):
                return obj
            time.sleep(2)
        raise TimeoutError("Revision not applied")

    def reconcile(self, sha):
        stamp = str(time.time_ns())
        for kind in ("gitrepository", "kustomization"):
            self.kube(
                "-n",
                "flux-system",
                "annotate",
                kind,
                "spike",
                "reconcile.fluxcd.io/requestedAt=" + stamp,
                "--overwrite",
            )
        until = time.monotonic() + 120
        while time.monotonic() < until:
            obj = json.loads(
                self.kube(
                    "-n", "flux-system", "get", "gitrepository", "spike", "-o", "json"
                )
            )
            if (
                obj.get("status", {})
                .get("artifact", {})
                .get("revision", "")
                .endswith(sha)
            ):
                self.kube(
                    "-n",
                    "flux-system",
                    "annotate",
                    "kustomization",
                    "spike",
                    "reconcile.fluxcd.io/requestedAt=" + str(time.time_ns()),
                    "--overwrite",
                )
                return
            time.sleep(2)
        save(self.results / "source-failure.json", obj)
        raise TimeoutError("Git source not fetched")

    def healthy(self, case):
        self.kube(
            "-n",
            "fulfillflow",
            "rollout",
            "status",
            "deployment/" + TARGET,
            "--timeout=120s",
        )
        env = os.environ.copy()
        env["CARRIER_ALPHA_WEBHOOK_SECRET"] = json.loads(
            (self.root / "private/values.json").read_text()
        )["alpha"]
        result = run(
            [
                sys.executable,
                self.root / "infra/scripts/verify_flow.py",
                "--base-url",
                "http://127.0.0.1:18742",
                "--output-dir",
                self.results / (case + "-functional"),
            ],
            env=env,
            timeout=90,
        )
        return json.loads(result)

    def execute(self):
        processes, handles = [], []
        try:
            if self.mode == "direct":
                # Qualification installed this source; do not compete with it.
                existing = self.kube(
                    "-n", "flux-system", "get", "kustomizations", "-o", "json"
                )
                if any(
                    x["metadata"]["name"] == "spike"
                    for x in json.loads(existing)["items"]
                ):
                    self.suspend(True)
            for name, args in [
                (
                    "tunnel",
                    self.k
                    + [
                        "-n",
                        "fulfillflow",
                        "port-forward",
                        "service/core",
                        "18742:8000",
                        "--address=127.0.0.1",
                    ],
                ),
                (
                    "git-http",
                    [
                        sys.executable,
                        Path(__file__).with_name("spike_git_source.py"),
                        "--root",
                        self.gitroot,
                    ],
                ),
            ]:
                log = (self.results / (name + ".log")).open("w")
                handles.append(log)
                processes.append(
                    subprocess.Popen(list(map(str, args)), stdout=log, stderr=log)
                )
            for port in (18742, 18743):
                for _ in range(20):
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            break
                    except OSError:
                        time.sleep(1)
                else:
                    raise TimeoutError("Local listener unavailable")
            sha, manifest = self.commit(self.mode + "-healthy")
            if self.mode == "flux":
                self.apply(
                    {
                        "apiVersion": "source.toolkit.fluxcd.io/v1",
                        "kind": "GitRepository",
                        "metadata": {"name": "spike", "namespace": "flux-system"},
                        "spec": {
                            "interval": "60s",
                            "url": "http://host.docker.internal:18743/desired.git",
                            "ref": {"branch": "main"},
                            "timeout": "20s",
                        },
                    }
                )
                self.apply(
                    {
                        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
                        "kind": "Kustomization",
                        "metadata": {"name": "spike", "namespace": "flux-system"},
                        "spec": {
                            "interval": "60s",
                            "path": "./",
                            "prune": False,
                            "sourceRef": {"kind": "GitRepository", "name": "spike"},
                            "wait": True,
                            "timeout": "120s",
                            "suspend": False,
                        },
                    }
                )
                self.suspend(False)
                self.reconcile(sha)
            else:
                self.apply(manifest)
            self.wait_revision(self.mode + "-healthy")
            self.rows.append(
                {"case": "healthy", "sha": sha, "functional": self.healthy("healthy")}
            )
            save(self.results / "results.json", self.rows)
            self.remaining_cases()
        finally:
            for process in reversed(processes):
                process.terminate()
                process.wait(timeout=10)
            for handle in handles:
                handle.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("direct", "flux"))
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    Experiment(args.output.resolve(), args.mode, args.attempt).execute()
