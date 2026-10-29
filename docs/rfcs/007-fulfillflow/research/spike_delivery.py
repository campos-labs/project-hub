"""Issue #102: isolated delivery research; never use a historical kubeconfig.

Python 3.12 + PyYAML 6.0.2 (the infra validation environment suffices).
prepare --infra-repo PATH --tools PATH --output NEW_EXTERNAL_DIRECTORY
bootstrap --output DIRECTORY
resume --output DIRECTORY
install-flux --output DIRECTORY
All generated manifests, credentials and observations stay outside this repository.
The pinned infra bootstrap is reused without source edits. Run the comparison
with spike_delivery_cases.py after resume and install-flux.
The disk guard checks the output drive, not free space inside Docker's virtual disk.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

INFRA = "c9dcaf745f126df8fe7902cb449391f63e9de3f8"
CLUSTER = "fulfillflow-spike-102"
MIN_FREE = 8 * 1024**3
FLUX = "2.6.4"  # Compatible representative for the pinned Kubernetes 1.34 lab.
FLUX_SHA = "991ecb38ac9013b02170a0c2fae116150510da80c426863ce6e3b63e3f8b55b1"


def command(args, **kwargs):
    return subprocess.run(
        [str(x) for x in args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        **kwargs,
    ).stdout


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def require_space(path):
    ancestor = path.resolve()
    while not ancestor.exists():
        ancestor = ancestor.parent
    if shutil.disk_usage(ancestor).free < MIN_FREE:
        raise RuntimeError("INSUFFICIENT_HOST_DISK_SPACE: at least 8 GiB free required")


def validate_export(out, inputs):
    archive = out / "infra.zip"
    if hashlib.sha256(archive.read_bytes()).hexdigest() != inputs["archive_sha256"]:
        raise ValueError("Archive changed")
    root = (out / "infra").resolve()
    with zipfile.ZipFile(archive) as bundle:
        expected = set()
        for item in bundle.infolist():
            if item.is_dir():
                continue
            path = (root / item.filename).resolve()
            if not path.is_relative_to(root) or path.is_symlink():
                raise ValueError("Unsafe export path")
            expected.add(path)
            if not path.is_file() or path.read_bytes() != bundle.read(item):
                raise ValueError("Export changed")
        actual = {p.resolve() for p in root.rglob("*") if p.is_file()}
        if actual != expected:
            raise ValueError("Unexpected export files")


def prepare(args):
    out = args.output.resolve()
    if out.exists() or out.is_relative_to(Path(__file__).resolve().parents[4]):
        raise ValueError("A new output directory outside the checkout is required")
    require_space(out)
    out.mkdir(parents=True)
    archive = out / "infra.zip"
    command(
        ["git", "-C", args.infra_repo, "archive", "--format=zip", "-o", archive, INFRA]
    )
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(out / "infra")
    write(
        out / "inputs.json",
        {
            "infra_sha": INFRA,
            "tools": str(args.tools.resolve()),
            "cluster": CLUSTER,
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "protocol": {
                "alternatives": ["direct", "flux"],
                "cases": [
                    "healthy",
                    "startup_failure",
                    "delivery_interruption",
                    "emergency_change",
                ],
                "owner": "direct executor or Flux, never concurrent template writers",
                "recovery": "commit healthy desired state before applying/resuming reconciliation",
                "schema": "fixed",
                "automatic_retry": False,
                "readiness_seconds": 120,
                "functional_seconds": 60,
                "rejected_reapplication_observation_seconds": 130,
                "decision": "prefer simpler path if both satisfy functional and coherence checks",
            },
        },
    )


def bootstrap(args):
    out = args.output.resolve()
    inputs = json.loads((out / "inputs.json").read_text())
    if inputs["infra_sha"] != INFRA or inputs["cluster"] != CLUSTER:
        raise ValueError("Unexpected environment identity")
    require_space(out)
    validate_export(out, inputs)
    # Keep the pinned source export immutable, including import bytecode.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(out / "infra"))
    env = importlib.import_module("scripts.scale_environment")
    env.CLUSTER = CLUSTER
    tools = Path(inputs["tools"])
    env.tools = lambda: (tools / "kubectl/kubectl.exe", tools / "kind/kind.exe")
    original_command = env.command

    def guarded_command(argv, **kwargs):
        # The reused helper stops its node in finally, even if creation failed.
        # Avoid masking the primary error when no node exists to stop.
        if list(map(str, argv)) == [
            "docker",
            "stop",
            "--timeout",
            "30",
            CLUSTER + "-control-plane",
        ]:
            names = original_command(["docker", "ps", "-a", "--format", "{{.Names}}"])
            if CLUSTER + "-control-plane" not in names.splitlines():
                return ""
        return original_command(argv, **kwargs)

    env.command = guarded_command
    try:
        env.bootstrap(out / "private", out / "bootstrap")
    except Exception as error:
        write(
            out / "preparation-failure.json",
            {"stage": "bootstrap", "error_type": type(error).__name__},
        )
        raise


class GuardTests(unittest.TestCase):
    def test_drift_exit_status_preserves_result(self):
        for expectation, exit_code in ((True, 0), (False, 1)):
            with (
                self.subTest(expectation=expectation),
                tempfile.TemporaryDirectory() as folder,
            ):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        "import json,sys; from pathlib import Path; "
                        "from spike_delivery_drift import record_result; "
                        "record_result(Path(sys.argv[1]), [{'previous': True}], "
                        "{'expectation_met': json.loads(sys.argv[2])})",
                        folder,
                        json.dumps(expectation),
                    ],
                    cwd=Path(__file__).resolve().parent,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, exit_code, result.stderr)
                self.assertEqual(
                    json.loads((Path(folder) / "results.json").read_text()),
                    [{"previous": True}, {"expectation_met": expectation}],
                )

    def test_failed_flux_prerequisite_prevents_install(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            archive = out / ("flux-" + FLUX + ".zip")
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("flux.exe", b"test executable")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with (
                patch(__name__ + ".FLUX_SHA", digest),
                patch(__name__ + ".verify_namespace"),
                patch(
                    __name__ + ".command",
                    side_effect=subprocess.CalledProcessError(1, "check"),
                ) as invoked,
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    install_flux(argparse.Namespace(output=out))
                self.assertEqual(invoked.call_count, 1)
                self.assertEqual(invoked.call_args.args[0][1:3], ["check", "--pre"])

    def test_low_space_blocks_before_creation(self):
        with patch.object(
            shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 100, 0)
        ):
            with self.assertRaisesRegex(RuntimeError, "INSUFFICIENT_HOST_DISK_SPACE"):
                require_space(Path.cwd())

    def test_enough_space(self):
        with patch.object(
            shutil,
            "disk_usage",
            return_value=shutil._ntuple_diskusage(MIN_FREE, 0, MIN_FREE),
        ):
            require_space(Path.cwd())

    def test_export_integrity(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            root = out / "infra"
            root.mkdir()
            source = root / "source.py"
            source.write_bytes(b"original")
            with zipfile.ZipFile(out / "infra.zip", "w") as bundle:
                bundle.writestr("source.py", b"original")
            inputs = {
                "archive_sha256": hashlib.sha256(
                    (out / "infra.zip").read_bytes()
                ).hexdigest()
            }
            validate_export(out, inputs)
            source.write_bytes(b"modified")
            with self.assertRaisesRegex(ValueError, "Export changed"):
                validate_export(out, inputs)
            source.write_bytes(b"original")
            (root / "extra.py").write_bytes(b"unexpected")
            with self.assertRaisesRegex(ValueError, "Unexpected export files"):
                validate_export(out, inputs)
            with self.assertRaisesRegex(ValueError, "Archive changed"):
                validate_export(out, {"archive_sha256": "0" * 64})


def selftest(args):
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(GuardTests)
    )
    if not result.wasSuccessful():
        raise SystemExit(1)


def kube_args(out):
    inputs = json.loads((out / "inputs.json").read_text())
    return [
        Path(inputs["tools"]) / "kubectl/kubectl.exe",
        "--kubeconfig",
        out / "private/kubeconfig",
        "--context",
        "kind-" + CLUSTER,
        "--request-timeout=20s",
    ]


def verify_namespace(out):
    identity = json.loads((out / "private/identity.json").read_text())
    namespace = json.loads(
        command(kube_args(out) + ["get", "namespace", "fulfillflow", "-o", "json"])
    )
    if namespace["metadata"]["uid"] != identity["namespace_uid"]:
        raise ValueError("Namespace identity changed")


def resume(args):
    out = args.output.resolve()
    identity = json.loads((out / "private/identity.json").read_text())
    node = CLUSTER + "-control-plane"
    actual = json.loads(command(["docker", "inspect", node]))[0]
    if actual["Id"] != identity["container_id"]:
        raise ValueError("Container identity changed")
    command(["docker", "start", node])
    command(
        kube_args(out)
        + ["wait", "--for=condition=Ready", "node", node, "--timeout=120s"]
    )
    verify_namespace(out)


def install_flux(args):
    out = args.output.resolve()
    verify_namespace(out)
    archive = out / ("flux-" + FLUX + ".zip")
    if not archive.exists():
        urllib.request.urlretrieve(
            f"https://github.com/fluxcd/flux2/releases/download/v{FLUX}/flux_{FLUX}_windows_amd64.zip",
            archive,
        )
    if hashlib.sha256(archive.read_bytes()).hexdigest() != FLUX_SHA:
        raise ValueError("Flux archive checksum mismatch")
    destination = out / ("flux-" + FLUX)
    with zipfile.ZipFile(archive) as bundle:
        binary = bundle.read("flux.exe")
    destination.mkdir(exist_ok=True)
    executable = destination / "flux.exe"
    if executable.exists() and executable.read_bytes() != binary:
        raise ValueError("Flux executable changed")
    if not executable.exists():
        executable.write_bytes(binary)
    target = [
        "--kubeconfig",
        out / "private/kubeconfig",
        "--context",
        "kind-" + CLUSTER,
    ]
    # Never install after a failed prerequisite check.
    command([executable, "check", "--pre", *target])
    installed = command(
        [
            executable,
            "install",
            "--components=source-controller,kustomize-controller",
            "--timeout=3m",
            *target,
        ]
    )
    (out / "flux-install.log").write_text(installed, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--infra-repo", type=Path, required=True)
    prep.add_argument("--tools", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    boot = sub.add_parser("bootstrap")
    boot.add_argument("--output", type=Path, required=True)
    sub.add_parser("selftest")
    for name in ("resume", "install-flux"):
        sub.add_parser(name).add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    {
        "prepare": prepare,
        "bootstrap": bootstrap,
        "selftest": selftest,
        "resume": resume,
        "install-flux": install_flux,
    }[args.action](args)


if __name__ == "__main__":
    main()
