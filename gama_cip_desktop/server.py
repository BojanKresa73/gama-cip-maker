"""Local HTTP API on 127.0.0.1 used by the program window.

Every API call needs the per-launch token (header X-Token, or ?t= for images and
downloads), so other web pages open in a browser cannot drive the program.
"""
import hashlib
import json
import mimetypes
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import traceback
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from gama_cip.engine import ghostscript, process_pdf
from gama_cip.exporter import export_zip
from gama_cip.names import ascii_name

from . import __version__
from .store import JobStore, ProfileStore, blockers, data_dir, engine_settings

MAX_PDF = 500 * 1024 * 1024


def web_folder():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "gama_cip_desktop" / "web"
    return Path(__file__).resolve().parent / "web"


def result_files(result):
    names = set()
    for page in (result or {}).get("pages", []):
        for key in ("png", "thumbnail", "view"):
            if page.get(key):
                names.add(page[key])
        for c in page.get("channels", []):
            for key in ("thumbnail", "view"):
                if c.get(key):
                    names.add(c[key])
    return names


class App:
    def __init__(self, root=None):
        self.root = Path(root) if root else data_dir()
        self.profiles = ProfileStore(self.root)
        self.jobs = JobStore(self.root)
        self.token = secrets.token_urlsafe(24)
        self.queue = queue.Queue()
        self.on_show = None   # set by the desktop window: bring the window to the front
        self.notify = None    # set by the desktop window: Windows notification (title, text)
        self.worker = threading.Thread(target=self._work, daemon=True, name="cip-worker")
        self.worker.start()
        for job in self.jobs.all():
            if job["status"] == "queued":
                self.queue.put(job["id"])
        from .hotfolder import HotFolders
        from .updater import Updater
        self.hot = HotFolders(self)
        self.updater = Updater(self, __version__)

    def ghostscript_status(self):
        try:
            gs = ghostscript()
            version = subprocess.run([gs, "--version"], capture_output=True, text=True, timeout=15,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
            return {"ok": True, "version": version}
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return {"ok": False, "error": str(exc)}

    def submit(self, filename, data, profile_id, hot=None):
        if not re.search(r"\.pdf$", filename, re.I):
            raise ValueError("Dozvoljeni su samo PDF fajlovi.")
        if b"%PDF-" not in data[:1024]:
            raise ValueError("Fajl nije prepoznat kao PDF.")
        profile = self.profiles.get(profile_id)
        if not profile:
            raise ValueError("Profil ne postoji.")
        problems = blockers(profile)
        if problems:
            raise ValueError("Profil nije spreman za obradu: " + "; ".join(problems))
        job = self.jobs.create(filename, data, profile, hot=hot)
        self.queue.put(job["id"])
        return job

    def retry(self, job_id):
        job = self.jobs.get(job_id)
        if not job or job["status"] != "failed":
            raise ValueError("Ponovo se može pokrenuti samo neuspeo posao.")
        self.jobs.update(job_id, status="queued", stage="U redu čekanja", error=None, result=None)
        self.queue.put(job_id)
        return self.jobs.get(job_id)

    def _work(self):
        while True:
            job_id = self.queue.get()
            job = self.jobs.get(job_id)
            if not job or job["status"] != "queued":
                continue
            folder = self.jobs.path(job_id)
            results = folder / "results"
            shutil.rmtree(results, ignore_errors=True)
            self.jobs.update(job_id, status="processing", stage="Priprema")
            try:
                result = process_pdf(folder / "source.pdf", engine_settings(job["profile"]), results,
                                     lambda stage: self.jobs.update(job_id, stage=stage))
                self.jobs.update(job_id, status="completed", stage="Spremno za pregled i izvoz", result=result, error=None)
            except Exception as exc:  # noqa: BLE001 - message goes to the user
                if not isinstance(exc, ValueError):
                    traceback.print_exc()
                self.jobs.update(job_id, status="failed", stage="Obrada nije uspela", error=str(exc)[:3000])
            try:
                self.hot.finished(self.jobs.get(job_id))
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    @staticmethod
    def side_for(job):
        """Front like PressPerCent, Back only when the file name says so; profile default otherwise."""
        from gama_cip.names import side_from_name
        return side_from_name(job["filename"]) or (job.get("profile") or {}).get("side") or "Front"

    def export(self, job_id, selections, cip_job_id, side="auto"):
        job = self.jobs.get(job_id)
        if job and side not in ("Front", "Back"):
            side = self.side_for(job)
        if not job or job["status"] != "completed":
            raise ValueError("Posao nema završen obračun.")
        if not job["profile"].get("export_enabled"):
            raise ValueError("Izvoz nije omogućen u profilu ovog posla.")
        results = self.jobs.path(job_id) / "results"
        key = hashlib.sha256(json.dumps({"s": selections, "j": cip_job_id, "side": side}, sort_keys=True).encode()).hexdigest()[:16]
        target = results / f"export-{key}.zip"
        if not target.exists():
            bundle = export_zip(job["result"], results, selections, cip_job_id, side, job["filename"])
            bundle.replace(target)
        stem = ascii_name(Path(job["filename"]).stem, limit=80)
        profile = job["result"]["profile"]
        with zipfile.ZipFile(target) as archive:
            machine = [n for n in archive.namelist() if "/" not in n]
            if len(machine) == 1 and machine[0].lower().endswith((".ppf", ".pdf")):
                # Same deliverable as PressPerCent: the .PPF (or the key-report PDF) itself.
                ext = Path(machine[0]).suffix
                single = results / f"export-{key}{ext}"
                single.write_bytes(archive.read(machine[0]))
                return {"name": single.name, "suggested": machine[0], "kind": ext[1:].lower()}
        return {"name": target.name, "suggested": f"{stem}_{cip_job_id.strip()}_{side}.zip", "kind": "zip"}


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = "GamaCIP"

    def log_message(self, *_):
        pass

    # --- helpers -------------------------------------------------------
    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, download_name=None):
        data = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        if download_name:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(download_name))
        self.end_headers()
        self.wfile.write(data)

    def body(self, limit=5 * 1024 * 1024):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("Fajl je prevelik (najviše 500 MB).")
        return self.rfile.read(length)

    def authorized(self, query):
        supplied = self.headers.get("X-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(supplied, self.app.token)

    def route(self, method):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
        if method == "GET" and (not parts or parts == ["index.html"]):
            return self.send_file(web_folder() / "index.html")
        if not parts or parts[0] != "api":
            return self.send_json({"error": "Nije pronađeno."}, 404)
        if not self.authorized(query):
            return self.send_json({"error": "Nedozvoljen pristup."}, 403)
        app = self.app
        p = parts[1:]
        try:
            if method == "GET" and p == ["state"]:
                return self.send_json({"version": __version__, "ghostscript": app.ghostscript_status(), "data_dir": str(app.root)})
            if p == ["hot"]:
                if method == "GET":
                    return self.send_json(app.hot.status())
                if method == "POST":
                    body = json.loads(self.body() or b"{}")
                    return self.send_json(app.hot.configure(body.get("root"), body.get("enabled"), body.get("keep_days")))
            if p == ["update"]:
                if method == "GET":
                    return self.send_json(app.updater.status() | {"updated_from": app.updater.updated_from})
                if method == "POST":
                    body = json.loads(self.body() or b"{}")
                    action = body.get("action")
                    if action == "check":
                        if app.updater.check():
                            threading.Thread(target=app.updater.download, daemon=True, name="cip-update-dl").start()
                        return self.send_json(app.updater.status())
                    if action == "auto":
                        return self.send_json(app.updater.set_auto(body.get("value")))
                    if action == "install":
                        return self.send_json({"message": app.updater.install(force=bool(body.get("force")))})
                    raise ValueError("Nepoznata radnja.")
            if p == ["activity"] and method == "GET":
                return self.send_json(app.hot.activity(int((query.get("since") or ["0"])[0] or 0)))
            if p == ["show"] and method == "POST":
                if app.on_show:
                    app.on_show()
                return self.send_json({"ok": True})
            if p == ["profiles"]:
                if method == "GET":
                    return self.send_json({"profiles": app.profiles.all()})
                if method == "POST":
                    body = json.loads(self.body() or b"{}")
                    return self.send_json({"profile": app.profiles.save(body.get("profile") or {}, body.get("original_id"))})
            if len(p) == 2 and p[0] == "profiles" and method == "DELETE":
                app.profiles.delete(p[1])
                return self.send_json({"ok": True})
            if p == ["jobs"]:
                if method == "GET":
                    arg = lambda k, d="": (query.get(k) or [d])[0]
                    return self.send_json(app.jobs.query(arg("q"), arg("printshop"), arg("status"), arg("source"),
                                                         int(arg("days", "0") or 0), int(arg("offset", "0") or 0),
                                                         min(500, int(arg("limit", "60") or 60))))
                if method == "POST":
                    name = (query.get("name") or ["dokument.pdf"])[0]
                    profile_id = (query.get("profile") or [""])[0]
                    job = app.submit(Path(name).name, self.body(MAX_PDF), profile_id)
                    return self.send_json({"job": job}, 201)
            if len(p) >= 2 and p[0] == "jobs":
                job_id = p[1]
                job = app.jobs.get(job_id)
                if not job:
                    return self.send_json({"error": "Posao nije pronađen."}, 404)
                if len(p) == 2 and method == "GET":
                    from gama_cip.names import job_id_from_name, side_from_name
                    extra = {"auto_side": app.side_for(job), "side_from_name": bool(side_from_name(job["filename"])),
                             "job_number": job_id_from_name(job["filename"])}
                    result = job.get("result") or {}
                    if (result.get("profile") or {}).get("report_enabled"):
                        from gama_cip.keys import ink_feed, zone_keys
                        prof = result["profile"]
                        extra["keys"] = {str(pg["page"]): {c["name"]: {"keys": zone_keys(c["values"], c["name"], prof),
                                                                       "feed": ink_feed(c.get("area_coverage", c["mean"]), c["name"], prof)}
                                                           for c in pg["channels"]} for pg in result.get("pages", [])}
                    return self.send_json({"job": job | extra})
                if len(p) == 2 and method == "DELETE":
                    app.jobs.delete(job_id)
                    return self.send_json({"ok": True})
                if p[2:] == ["retry"] and method == "POST":
                    return self.send_json({"job": app.retry(job_id)})
                if p[2:] == ["export"] and method == "POST":
                    body = json.loads(self.body() or b"{}")
                    cip_job_id = str(body.get("job_id") or "").strip()
                    xml = (job.get("result") or {}).get("profile", {}).get("xml_enabled", True)
                    if not cip_job_id and not xml:
                        cip_job_id = "PPF"  # PPF and the key report carry the file name as their job name
                    if not cip_job_id or len(cip_job_id) > 64:
                        raise ValueError("Unesite Job ID (do 64 znaka).")
                    return self.send_json(app.export(job_id, body.get("selections") or {}, cip_job_id, body.get("side") or "auto"))
                if len(p) == 4 and p[2] in ("files", "download") and method == "GET":
                    name = p[3]
                    allowed = result_files(job.get("result")) if p[2] == "files" else None
                    if "/" in name or "\\" in name or ".." in name:
                        raise ValueError("Neispravno ime fajla.")
                    if p[2] == "files" and name not in allowed:
                        return self.send_json({"error": "Fajl nije deo rezultata."}, 404)
                    if p[2] == "download" and not re.fullmatch(r"export-[0-9a-f]{16}\.(zip|PPF|pdf)", name):
                        return self.send_json({"error": "Neispravan izvoz."}, 404)
                    path = app.jobs.path(job_id) / "results" / name
                    if not path.exists():
                        return self.send_json({"error": "Fajl ne postoji."}, 404)
                    return self.send_file(path, (query.get("as") or [name])[0] if p[2] == "download" else None)
            return self.send_json({"error": "Nije pronađeno."}, 404)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self.send_json({"error": "Neočekivana greška: " + str(exc)}, 500)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")


def start(root=None, port=0):
    app = App(root)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="cip-http")
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={app.token}"
    return app, server, url
