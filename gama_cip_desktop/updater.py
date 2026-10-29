"""Automatic updates from the public installer repository.

Flow: check (every 6 h) -> download installer + SHA256SUMS.txt -> verify SHA-256 ->
install when no job is running -> the program exits, the installer runs silently and
starts the program again with --after-update.

Nothing here may disturb processing: every network or file error only changes the
update state shown in the window and is retried at the next check.
"""
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger("gama.cip.update")
REPOSITORY = "BojanKresa73/gama-cip-maker-instaler"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
CHECK_EVERY = 6 * 3600
FIRST_CHECK_AFTER = 20
ALLOWED_HOSTS = {"api.github.com", "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
INSTALLER = re.compile(r"^GamaCIPMaker-Setup-(\d+(?:\.\d+){1,3})\.exe$")


def parse_version(text):
    numbers = re.findall(r"\d+", str(text))
    return tuple(int(n) for n in numbers[:4]) + (0,) * (4 - min(len(numbers), 4))


class Updater:
    def __init__(self, app, current, quit_program=None):
        self.app = app
        self.current = current
        self.quit_program = quit_program        # set by the window: closes the program cleanly
        self.window_visible = lambda: True      # set by the window
        self.url = os.environ.get("GAMA_CIP_UPDATE_URL") or RELEASES_URL
        self.folder = Path(app.root) / "updates"
        self.settings_path = Path(app.root) / "updates.json"
        self.settings = self._load()
        self.lock = threading.RLock()
        self.state = {"current": current, "status": "idle", "latest": None, "notes": "", "progress": 0,
                      "error": None, "checked_at": None, "auto_install": self.settings.get("auto_install", True)}
        self.installer = None
        self.stop = threading.Event()
        self.updated_from = None
        previous = self.settings.get("last_version")
        if previous and parse_version(previous) < parse_version(current):
            self.updated_from = previous
        self.settings["last_version"] = current
        self._save()

    # ------------------------------------------------------------ settings
    def _load(self):
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            tmp = self.settings_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.settings_path)
        except OSError:
            LOG.exception("Podešavanja ažuriranja nisu sačuvana")

    def set_auto(self, value):
        self.settings["auto_install"] = bool(value)
        self._save()
        with self.lock:
            self.state["auto_install"] = bool(value)
        return self.status()

    def status(self):
        with self.lock:
            return dict(self.state)

    def _set(self, **values):
        with self.lock:
            self.state.update(values)

    # ------------------------------------------------------------ network
    def _open(self, url, timeout=30):
        parts = urlsplit(url)
        testing = "GAMA_CIP_UPDATE_URL" in os.environ
        if not (parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS) and not (testing and parts.scheme in ("file", "http")):
            raise ValueError(f"Nedozvoljena adresa za ažuriranje: {parts.scheme}://{parts.hostname}")
        req = Request(url, headers={"User-Agent": f"GamaCIPMaker/{self.current}", "Accept": "application/octet-stream"
                                    if "/releases/assets/" in url else "application/vnd.github+json"})
        return urlopen(req, timeout=timeout)

    def check(self):
        """Ask GitHub for the newest release; returns True if a newer installer exists."""
        self._set(status="checking", error=None)
        try:
            with self._open(self.url, timeout=20) as response:
                release = json.load(response)
            latest = release.get("tag_name", "").lstrip("vV")
            assets = {a["name"]: a for a in release.get("assets", [])}
            installer = next((a for n, a in assets.items() if INSTALLER.match(n)), None)
            sums = assets.get("SHA256SUMS.txt")
            self._set(checked_at=time.time(), latest=latest, notes=(release.get("body") or "")[:4000])
            if not latest or parse_version(latest) <= parse_version(self.current):
                self._set(status="uptodate")
                return False
            if not installer or not sums:
                raise ValueError("Objava nove verzije nema instaler ili SHA256SUMS.txt.")
            self.installer = {"name": installer["name"], "url": installer["browser_download_url"], "size": installer.get("size"),
                              "sums": sums["browser_download_url"], "version": latest}
            self._set(status="available")
            LOG.info("Dostupna nova verzija %s (trenutna %s)", latest, self.current)
            return True
        except Exception as exc:  # noqa: BLE001 - offline, blocked, rate limited...
            LOG.warning("Provera ažuriranja nije uspela: %s", exc)
            self._set(status="error", error=f"Provera nije uspela: {exc}", checked_at=time.time())
            return False

    def download(self):
        info = self.installer
        if not info:
            return None
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / info["name"]
        try:
            self._set(status="downloading", progress=0)
            with self._open(info["sums"]) as response:
                sums = response.read().decode("utf-8", "replace")
            expected = next((line.split()[0].lower() for line in sums.splitlines()
                             if line.strip().endswith(info["name"])), None)
            if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("SHA256SUMS.txt ne sadrži kontrolni zbir instalera.")
            if target.exists() and self._sha256(target) == expected:
                self._ready(target, info)
                return target
            part = target.with_suffix(".part")
            digest, total, size = hashlib.sha256(), 0, info.get("size") or 0
            with self._open(info["url"], timeout=60) as response, part.open("wb") as fh:
                while block := response.read(1024 * 1024):
                    fh.write(block)
                    digest.update(block)
                    total += len(block)
                    if size:
                        self._set(progress=min(99, int(total * 100 / size)))
            if digest.hexdigest() != expected:
                part.unlink(missing_ok=True)
                raise ValueError("Preuzeti instaler nije ispravan (SHA-256 se ne poklapa). Biće preuzet ponovo.")
            os.replace(part, target)
            for old in self.folder.glob("GamaCIPMaker-Setup-*.exe"):
                if old != target:
                    old.unlink(missing_ok=True)
            self._ready(target, info)
            return target
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Preuzimanje ažuriranja nije uspelo: %s", exc)
            self._set(status="error", error=f"Preuzimanje nije uspelo: {exc}")
            return None

    def _ready(self, target, info):
        self.installer["path"] = str(target)
        self._set(status="ready", progress=100, latest=info["version"])
        LOG.info("Verzija %s je preuzeta i proverena: %s", info["version"], target)
        if self.app.notify:
            try:
                self.app.notify("Gama CIP Maker", f"Verzija {info['version']} je spremna"
                                + (" i biće instalirana kad nema poslova." if self.state["auto_install"] else " — otvorite program da je instalirate."))
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    # ------------------------------------------------------------ install
    def busy(self):
        """True while any PDF is waiting or being processed."""
        if any(j["status"] in ("queued", "processing") for j in self.app.jobs.all()):
            return True
        hot = self.app.hot
        if hot.enabled:
            for item in hot.layout().values():
                for folder in (item["in"], item["in"] / ".u_obradi"):
                    if folder.is_dir() and any(f.suffix.lower() == ".pdf" for f in folder.iterdir() if f.is_file()):
                        return True
        return False

    def launcher_script(self, installer, pid):
        """PowerShell (UTF-16 encoded command, so Serbian letters in paths are safe):
        wait until this program has exited, then run the installer silently."""
        quote = lambda s: "'" + str(s).replace("'", "''") + "'"
        log = Path(installer).with_suffix(".log")
        args = f"/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /LOG=\"{log}\""
        return (f"Wait-Process -Id {int(pid)} -Timeout 120 -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1; "
                f"Start-Process -FilePath {quote(installer)} -ArgumentList {quote(args)}")

    def install(self, force=False):
        """Start the silent installer and close the program. Returns a message for the window."""
        info = self.installer or {}
        path = info.get("path")
        if self.status()["status"] != "ready" or not path or not Path(path).is_file():
            raise ValueError("Nova verzija još nije preuzeta.")
        if not force and self.busy():
            raise ValueError("Obrada je u toku. Instalacija će krenuti kad se poslovi završe.")
        if sys.platform != "win32":
            raise ValueError("Automatska instalacija radi samo na Windows-u.")
        import base64
        script = self.launcher_script(path, os.getpid())
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0) \
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                          "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                         creationflags=flags, close_fds=True)
        self._set(status="installing")
        LOG.info("Pokrenuta instalacija verzije %s; program se zatvara", info.get("version"))
        if self.quit_program:
            threading.Timer(0.5, self.quit_program).start()
        return f"Instaliram verziju {info.get('version')}. Program će se ponovo otvoriti za oko pola minuta."

    # ------------------------------------------------------------ background
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="cip-update").start()

    def _loop(self):
        if self.stop.wait(FIRST_CHECK_AFTER):
            return
        last_check = 0
        while not self.stop.is_set():
            if time.time() - last_check >= CHECK_EVERY or self.status()["status"] == "error" and time.time() - last_check > 1800:
                last_check = time.time()
                if self.check():
                    self.download()
            st = self.status()
            if st["status"] == "ready" and st["auto_install"] and not self.window_visible() and not self.busy():
                try:
                    self.install()
                    return
                except Exception:  # noqa: BLE001
                    LOG.exception("Automatska instalacija nije pokrenuta")
            self.stop.wait(60)
