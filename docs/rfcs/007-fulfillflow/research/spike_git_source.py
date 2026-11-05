"""Loopback smart-HTTP Git source for the isolated delivery Spike.

Only upload-pack (read) is exposed. The repository must contain synthetic
deployment manifests, never credentials. Git writes remain local CLI operations.
python spike_git_source.py --root EXTERNAL_DIRECTORY --port 18743
"""

import argparse
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def serve(root, port):
    class Handler(BaseHTTPRequestHandler):
        def handle_git(self):
            url = urlsplit(self.path)
            if url.path not in (
                "/desired.git/info/refs",
                "/desired.git/git-upload-pack",
            ):
                self.send_error(404)
                return
            if url.query not in ("", "service=git-upload-pack"):
                self.send_error(403)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 1048576:
                self.send_error(413)
                return
            env = os.environ.copy()
            env.update(
                {
                    "GIT_PROJECT_ROOT": str(root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": url.path,
                    "QUERY_STRING": url.query,
                    "REQUEST_METHOD": self.command,
                    "CONTENT_LENGTH": str(length),
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "REMOTE_ADDR": self.client_address[0],
                }
            )
            result = subprocess.run(
                ["git", "http-backend"],
                input=self.rfile.read(length),
                env=env,
                capture_output=True,
                timeout=20,
            )
            if result.returncode:
                self.send_error(502)
                return
            headers, body = result.stdout.split(b"\r\n\r\n", 1)
            parsed = [line.decode().split(":", 1) for line in headers.split(b"\r\n")]
            status = next(
                (int(v.strip().split()[0]) for k, v in parsed if k == "Status"), 200
            )
            self.send_response(status)
            for key, value in parsed:
                if key != "Status":
                    self.send_header(key, value.strip())
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = handle_git
        do_POST = handle_git

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18743)
    args = parser.parse_args()
    serve(args.root.resolve(), args.port)
