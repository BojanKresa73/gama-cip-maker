"""Automatic update: check, download, SHA-256 verification and the safety rules, against a fake release server."""
import base64
import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gama_cip_desktop import updater as U
from gama_cip_desktop.server import App


class FakeGitHub:
    def __init__(self, version="9.9.0", installer=b"MZ" + b"x" * 300_000, sums_ok=True):
        files = {}
        name = f"GamaCIPMaker-Setup-{version}.exe"
        files["/dl/" + name] = installer
        digest = hashlib.sha256(installer if sums_ok else b"drugo").hexdigest()
        files["/dl/SHA256SUMS.txt"] = f"{digest}  {name}\n".encode()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == "/release":
                    body = json.dumps({"tag_name": "v" + version, "body": "Novo: automatsko ažuriranje",
                                       "assets": [{"name": n.split("/")[-1], "size": len(d),
                                                   "browser_download_url": outer.base + n} for n, d in files.items()]}).encode()
                elif self.path in files:
                    body = files[self.path]
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Updates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gama-Đorđe-")
        self.servers = []

    def tearDown(self):
        os.environ.pop("GAMA_CIP_UPDATE_URL", None)
        for s in self.servers:
            s.close()
        self.tmp.cleanup()

    def app(self, **fake):
        gh = FakeGitHub(**fake)
        self.servers.append(gh)
        os.environ["GAMA_CIP_UPDATE_URL"] = gh.base + "/release"
        app = App(Path(self.tmp.name) / "podaci Šćž")
        app.hot.stop.set()
        return app

    def test_version_compare(self):
        self.assertGreater(U.parse_version("1.10.0"), U.parse_version("1.9.9"))
        self.assertEqual(U.parse_version("v1.2"), U.parse_version("1.2.0"))

    def test_newer_version_downloaded_and_verified(self):
        app = self.app()
        up = app.updater
        self.assertTrue(up.check())
        self.assertEqual(up.status()["latest"], "9.9.0")
        self.assertIn("automatsko", up.status()["notes"])
        target = up.download()
        self.assertTrue(target and target.exists())
        self.assertEqual(up.status()["status"], "ready")
        self.assertEqual(target.parent, Path(app.root) / "updates")

    def test_corrupt_download_rejected(self):
        app = self.app(sums_ok=False)
        up = app.updater
        self.assertTrue(up.check())
        self.assertIsNone(up.download())
        self.assertEqual(up.status()["status"], "error")
        self.assertIn("SHA-256", up.status()["error"])
        self.assertEqual(list((Path(app.root) / "updates").glob("*.exe")), [])
        with self.assertRaises(ValueError):
            up.install(force=True)   # nothing verified -> nothing to install

    def test_same_or_older_version_is_ignored(self):
        app = self.app(version="1.0.0")
        self.assertFalse(app.updater.check())
        self.assertEqual(app.updater.status()["status"], "uptodate")

    def test_offline_is_harmless(self):
        os.environ["GAMA_CIP_UPDATE_URL"] = "http://127.0.0.1:9/release"
        app = App(Path(self.tmp.name) / "podaci")
        app.hot.stop.set()
        self.assertFalse(app.updater.check())
        self.assertEqual(app.updater.status()["status"], "error")

    def test_only_github_https_in_production(self):
        os.environ.pop("GAMA_CIP_UPDATE_URL", None)
        app = App(Path(self.tmp.name) / "podaci")
        app.hot.stop.set()
        for url in ("http://github.com/x", "https://evil.example.com/x.exe", "file:///C:/x.exe"):
            with self.assertRaises(ValueError):
                app.updater._open(url)

    def test_waits_while_jobs_run(self):
        app = self.app()
        up = app.updater
        up.check()
        up.download()
        app.jobs.create("u radu.pdf", b"%PDF-1.4", {"name": "x"})   # queued job
        with self.assertRaises(ValueError) as ctx:
            up.install()
        self.assertIn("Obrada je u toku", str(ctx.exception))

    def test_launcher_handles_serbian_paths(self):
        app = self.app()
        script = app.updater.launcher_script(r"C:\Users\Đorđe Šćž\AppData\Local\GamaCIP\updates\Setup's.exe", 1234)
        self.assertIn("Wait-Process -Id 1234", script)
        self.assertIn("'C:\\Users\\Đorđe Šćž\\AppData\\Local\\GamaCIP\\updates\\Setup''s.exe'", script)
        self.assertIn("/VERYSILENT", script)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        self.assertEqual(base64.b64decode(encoded).decode("utf-16-le"), script)

    def test_remembers_previous_version(self):
        app = self.app()
        app.updater.settings["last_version"] = "1.0.0"
        app.updater._save()
        again = App(app.root)
        again.hot.stop.set()
        self.assertEqual(again.updater.updated_from, "1.0.0")


if __name__ == "__main__":
    unittest.main()
