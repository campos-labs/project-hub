"""Supplement #102: one drift case, direct control then active Flux.

Reuse an existing healthy run and its synthetic Flux Git repository.
--output RUN --git-root RUN/flux-evaluated-01-git --evidence NEW_DIRECTORY
No Git writes. Observe 180 seconds per arm (three configured 60-second intervals).
Only preparation/cleanup may suspend Flux or apply the desired manifest.
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from spike_delivery import kube_args, verify_namespace
from spike_delivery_cases import run, save


def main(args):
    root, evidence = args.output.resolve(), args.evidence.resolve()
    verify_namespace(root)
    evidence.mkdir(parents=True, exist_ok=False)
    k = kube_args(root)
    gitroot = args.git_root.resolve()
    manifest = gitroot / "work/deployment.json"
    desired = json.loads(manifest.read_text())
    expected = desired["spec"]["template"]["metadata"]["annotations"]["spike/revision"]
    sha = run(["git", "-C", gitroot / "work", "rev-parse", "HEAD"]).strip()
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    save(
        evidence / "protocol.json",
        {
            "git_sha": sha,
            "manifest_sha256": digest,
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "window_seconds": 180,
            "sample_seconds": 2,
            "flux_interval": "60s",
            "order": ["direct", "flux"],
            "field": "spec.template.metadata.annotations.spike/revision",
            "criteria": "direct retains injected drift; Flux restores Git value without intervention; functional verifier succeeds afterward",
            "observation": "No Git writes, apply, suspension change or manual reconcile during either window. Full window even if repaired early.",
        },
    )

    def kube(*a):
        return run(k + list(a))

    def get(kind, name, ns):
        return json.loads(kube("-n", ns, "get", kind, name, "-o", "json"))

    def suspend(value):
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

    def annotation(obj):
        return obj["spec"]["template"]["metadata"]["annotations"]["spike/revision"]

    def functional(label):
        kube(
            "-n",
            "fulfillflow",
            "rollout",
            "status",
            "deployment/notifications-worker",
            "--timeout=120s",
        )
        env = os.environ.copy()
        env["CARRIER_ALPHA_WEBHOOK_SECRET"] = json.loads(
            (root / "private/values.json").read_text()
        )["alpha"]
        result = json.loads(
            run(
                [
                    sys.executable,
                    root / "infra/scripts/verify_flow.py",
                    "--base-url",
                    "http://127.0.0.1:18742",
                    "--output-dir",
                    evidence / label,
                ],
                env=env,
                timeout=90,
            )
        )
        if not result["success"]:
            raise AssertionError("Functional verification failed")
        return result

    children, logs, rows = [], [], []
    try:
        for name, command in [
            (
                "git",
                [
                    sys.executable,
                    Path(__file__).with_name("spike_git_source.py"),
                    "--root",
                    gitroot,
                ],
            ),
            (
                "tunnel",
                k
                + [
                    "-n",
                    "fulfillflow",
                    "port-forward",
                    "service/core",
                    "18742:8000",
                    "--address=127.0.0.1",
                ],
            ),
        ]:
            log = (evidence / (name + ".log")).open("w")
            logs.append(log)
            children.append(
                subprocess.Popen(list(map(str, command)), stdout=log, stderr=log)
            )
        for port in (18742, 18743):
            for _ in range(30):
                if any(p.poll() is not None for p in children):
                    raise RuntimeError("Helper exited")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        break
                except OSError:
                    time.sleep(1)
            else:
                raise TimeoutError("Helper not ready")

        for mode in ("direct", "flux"):
            suspend(mode == "direct")
            if mode == "direct":
                kube("apply", "-f", str(manifest))
            else:
                # Wait for natural reconciliation, not a reconcile annotation.
                until = time.monotonic() + 180
                while time.monotonic() < until:
                    state = get("kustomization", "spike", "flux-system")
                    if state.get("status", {}).get("lastAppliedRevision", "").endswith(
                        sha
                    ) and any(
                        c["type"] == "Ready" and c["status"] == "True"
                        for c in state.get("status", {}).get("conditions", [])
                    ):
                        break
                    time.sleep(2)
                else:
                    raise TimeoutError("Flux baseline not ready")
            functional(mode + "-baseline")
            state = get("kustomization", "spike", "flux-system")
            assert state["spec"]["interval"] == "60s"
            assert state["spec"].get("suspend", False) == (mode == "direct")
            before = get("deployment", "notifications-worker", "fulfillflow")
            assert annotation(before) == expected
            save(
                evidence / (mode + "-before.json"),
                {"deployment": before, "flux": state},
            )
            injected = "spike-drift-" + mode
            started = time.monotonic()
            kube(
                "-n",
                "fulfillflow",
                "patch",
                "deployment",
                "notifications-worker",
                "--field-manager=spike-drift",
                "--type=merge",
                "-p",
                json.dumps(
                    {
                        "spec": {
                            "template": {
                                "metadata": {
                                    "annotations": {"spike/revision": injected}
                                }
                            }
                        }
                    }
                ),
            )
            samples = []
            with (evidence / (mode + "-samples.jsonl")).open("w") as stream:
                while True:
                    obj = get("deployment", "notifications-worker", "fulfillflow")
                    sample = {
                        "elapsed": time.monotonic() - started,
                        "revision": annotation(obj),
                        "generation": obj["metadata"]["generation"],
                    }
                    samples.append(sample)
                    stream.write(json.dumps(sample) + "\n")
                    stream.flush()
                    if sample["elapsed"] >= 180:
                        break
                    time.sleep(2)
            after = get("deployment", "notifications-worker", "fulfillflow")
            save(
                evidence / (mode + "-after.json"),
                {
                    "deployment": after,
                    "flux": get("kustomization", "spike", "flux-system"),
                },
            )
            assert hashlib.sha256(manifest.read_bytes()).hexdigest() == digest
            assert (
                run(["git", "-C", gitroot / "work", "rev-parse", "HEAD"]).strip() == sha
            )
            corrected = next(
                (s["elapsed"] for s in samples if s["revision"] == expected), None
            )
            row = {
                "mode": mode,
                "first_correction_observed_seconds": corrected,
                "last_revision": annotation(after),
                "git_unchanged": True,
                "functional": functional(mode + "-after"),
                "expectation_met": (corrected is None and annotation(after) == injected)
                if mode == "direct"
                else annotation(after) == expected,
            }
            rows.append(row)
            save(evidence / "results.json", rows)
            # Restore the direct control only after its full observation and verifier.
            if mode == "direct":
                kube("apply", "-f", str(manifest))
        (evidence / "controller.log").write_text(
            kube(
                "-n",
                "flux-system",
                "logs",
                "deployment/kustomize-controller",
                "--since=20m",
            ),
            encoding="utf-8",
        )
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
        for log in logs:
            log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--git-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    main(parser.parse_args())
