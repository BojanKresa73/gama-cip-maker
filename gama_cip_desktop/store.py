"""Local storage for profiles and jobs (no cloud, no accounts).

Profiles may be incomplete (e.g. printshops still waiting for DI-Plot data);
they are kept and editable but cannot be used for processing until complete.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

from gama_cip.profiles import validate

TEXT_FIELDS = ("job_id", "signature", "sheet", "preview_url_prefix", "notes", "machine")
REQUIRED = {
    "machine": "Mašina",
    "plate_width_mm": "Širina ploče",
    "plate_height_mm": "Visina ploče",
    "zone_count": "Broj zona",
    "zone_width_mm": "Širina zone",
    "area_height_mm": "Visina zone boje",
    "shift_x_mm": "Pomak X",
    "top_mm": "Gornji ofset",
    "signature": "Signature",
    "sheet": "Sheet",
}


def data_dir():
    configured = os.environ.get("GAMA_CIP_DATA")
    if configured:
        base = Path(configured)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "GamaCIP"
    else:
        base = Path.home() / ".gama-cip"
    base.mkdir(parents=True, exist_ok=True)
    return base


def seed_folder():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "gama_cip_desktop" / "seed_profiles"
    return Path(__file__).resolve().parent / "seed_profiles"


def slug(text):
    text = text.lower()
    for a, b in (("š", "s"), ("đ", "dj"), ("č", "c"), ("ć", "c"), ("ž", "z")):
        text = text.replace(a, b)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:80] or "profil"


def blockers(p):
    """Human-readable reasons a profile cannot be used for processing; empty = usable."""
    xml = p.get("xml_enabled", True)
    out = [f"Nedostaje: {label}" for key, label in REQUIRED.items()
           if p.get(key) in (None, "") and (xml or key not in ("signature", "sheet"))]
    if not xml and not p.get("ppf_enabled"):
        out.append("Uključite bar jedan izlaz (XML ili PPF)")
    if not p.get("active"):
        out.append("Profil nije aktivan")
    if not out:
        try:
            validate(engine_settings(p))
        except ValueError as exc:
            out.append(str(exc))
    return out


def engine_settings(p):
    s = dict(p)
    for key in TEXT_FIELDS:
        if s.get(key) is None:
            s[key] = ""
    s.setdefault("validation_status", "unverified")
    if s.get("side") not in ("Front", "Back"):
        s["side"] = "Front"   # like PressPerCent; Back only from the file name
    return s


class ProfileStore:
    def __init__(self, root):
        self.folder = Path(root) / "profiles"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        if not any(self.folder.glob("*.json")):
            for src in seed_folder().glob("*.json"):
                shutil.copyfile(src, self.folder / src.name)

    def _path(self, identifier):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identifier or ""):
            raise ValueError("Neispravan ID profila.")
        return self.folder / (identifier + ".json")

    def all(self):
        out = []
        for f in sorted(self.folder.glob("*.json")):
            try:
                p = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append(self.describe(p))
        return sorted(out, key=lambda p: p["name"].lower())

    @staticmethod
    def describe(p):
        p = dict(p)
        p["blockers"] = blockers(p)
        p["usable"] = not p["blockers"]
        return p

    def get(self, identifier):
        path = self._path(identifier)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, p, original_id=None):
        p = {k: v for k, v in p.items() if k not in ("blockers", "usable")}
        name = (p.get("name") or "").strip()
        if not name:
            raise ValueError("Profil mora imati naziv.")
        if not (p.get("printshop") or "").strip():
            raise ValueError("Unesite naziv štamparije.")
        p["name"] = name
        p["id"] = p.get("id") or slug(name)
        p["schema_version"] = 1
        p.setdefault("coverage_dpi", 150)
        with self.lock:
            old = self.get(original_id or p["id"])
            if original_id and original_id != p["id"] and self.get(p["id"]):
                raise ValueError("Profil sa tim ID-jem već postoji.")
            if not original_id and old:
                raise ValueError("Profil sa tim ID-jem već postoji.")
            # Any change to geometry or output removes inherited verification.
            comparable = lambda d: {k: v for k, v in (d or {}).items() if k not in ("validation_status", "notes", "name", "active", "export_enabled", "job_id",
                                                                       "hot_folder", "hot_folder_name", "hot_copy_to")}
            if not old or comparable(old) != comparable(p):
                p["validation_status"] = "unverified"
            else:
                p["validation_status"] = old.get("validation_status", "unverified")
            if p.get("export_enabled") and blockers(p):
                p["export_enabled"] = False
            path = self._path(p["id"])
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            if original_id and original_id != p["id"]:
                self._path(original_id).unlink(missing_ok=True)
        return self.describe(p)

    def delete(self, identifier):
        with self.lock:
            self._path(identifier).unlink(missing_ok=True)


class JobStore:
    """Each job lives in its own folder: source.pdf, job.json and results/.

    A small index (jobs/index.json) keeps one summary per job, so the archive can be
    searched and filtered quickly even with thousands of jobs.
    """

    LIGHT_AFTER_DAYS = 90    # large preview files of older jobs are removed; the data stays

    def __init__(self, root, keep=5000):
        self.folder = Path(root) / "jobs"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.keep = keep
        self.lock = threading.RLock()
        self.index_path = self.folder / "index.json"
        self.index = self._load_index()
        # A job interrupted by closing the app is marked failed on next start.
        for summary in list(self.index.values()):
            if summary["status"] in ("queued", "processing"):
                self.update(summary["id"], status="failed", stage="Prekinuto", error="Program je zatvoren tokom obrade. Pokrenite ponovo.")
        self.lighten()

    # ------------------------------------------------------------ index
    @staticmethod
    def summary(job):
        from gama_cip.names import job_id_from_name, search_key
        result = job.get("result") or {}
        pages = result.get("pages") or []
        cov, spots = {}, set()
        for page in pages:
            for c in page.get("channels", []):
                if c["name"] in ("Cyan", "Magenta", "Yellow", "Black"):
                    cov.setdefault(c["name"], []).append(c["mean"])
                else:
                    spots.add(c["name"])
        profile = job.get("profile") or {}
        hot = job.get("hot")
        return {"id": job["id"], "filename": job.get("filename", ""), "created": job.get("created", 0),
                "status": job.get("status"), "stage": job.get("stage"), "error": (job.get("error") or "")[:300],
                "profile": {"id": profile.get("id"), "name": profile.get("name", ""), "printshop": profile.get("printshop", "")},
                "hot": {"base": hot.get("base")} if hot else None, "pages": len(pages),
                "job_number": job_id_from_name(job.get("filename", "")),
                "coverage": {k: sum(v) / len(v) for k, v in cov.items()}, "spots": sorted(spots),
                "light": bool(job.get("light")),
                "key": search_key(" ".join([job.get("filename", ""), profile.get("printshop", ""), profile.get("name", "")]))}

    def _load_index(self):
        dirs = {f.name for f in self.folder.iterdir() if f.is_dir() and re.fullmatch(r"[0-9a-f-]{36}", f.name)}
        try:
            index = json.loads(self.index_path.read_text(encoding="utf-8"))
            if set(index) == dirs:
                return index
        except (OSError, ValueError):
            pass
        index = {}   # rebuild once from the job folders (first start of this version, or a damaged index)
        for name in dirs:
            job = self.get(name)
            if job:
                index[name] = self.summary(job)
        self._save_index(index)
        return index

    def _save_index(self, index=None):
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index if index is not None else self.index, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.index_path)

    # ------------------------------------------------------------ jobs
    def path(self, job_id):
        return self.folder / str(uuid.UUID(str(job_id)))

    def create(self, filename, data, profile, hot=None):
        job_id = str(uuid.uuid4())
        folder = self.path(job_id)
        folder.mkdir(parents=True)
        (folder / "source.pdf").write_bytes(data)
        job = {"id": job_id, "filename": filename, "created": time.time(), "status": "queued", "stage": "U redu čekanja",
               "error": None, "profile": profile, "result": None, "size": len(data), "hot": hot}
        self._write(job)
        self.prune()
        return job

    def _write(self, job):
        target = self.path(job["id"]) / "job.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
        with self.lock:
            self.index[job["id"]] = self.summary(job)
            self._save_index()

    def get(self, job_id):
        try:
            return json.loads((self.path(job_id) / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def update(self, job_id, **values):
        with self.lock:
            job = self.get(job_id)
            if job:
                job.update(values)
                self._write(job)
            return job

    def all(self):
        """Job summaries, newest first."""
        with self.lock:
            return sorted(self.index.values(), key=lambda j: j["created"], reverse=True)

    def query(self, q="", printshop="", status="", source="", days=0, offset=0, limit=60):
        from gama_cip.names import search_key
        jobs = self.all()
        facets = {}
        for j in jobs:
            facets[j["profile"]["printshop"]] = facets.get(j["profile"]["printshop"], 0) + 1
        words = search_key(q).split()
        since = time.time() - days * 86400 if days else 0
        groups = {"ok": ("completed",), "error": ("failed",), "busy": ("queued", "processing")}
        out = [j for j in jobs
               if all(w in j["key"] or w in j["job_number"] for w in words)
               and (not printshop or j["profile"]["printshop"] == printshop)
               and (not status or j["status"] in groups.get(status, (status,)))
               and (not source or (source == "hot") == bool(j["hot"]))
               and j["created"] >= since]
        return {"total": len(out), "all": len(jobs), "jobs": out[offset:offset + limit],
                "printshops": sorted(([k, v] for k, v in facets.items() if k), key=lambda kv: kv[0].lower())}

    def delete(self, job_id):
        job = self.get(job_id)
        if job and job["status"] in ("queued", "processing"):
            raise ValueError("Posao se trenutno obrađuje.")
        shutil.rmtree(self.path(job_id), ignore_errors=True)
        with self.lock:
            self.index.pop(str(job_id), None)
            self._save_index()

    def prune(self):
        for job in self.all()[self.keep:]:
            if job["status"] not in ("queued", "processing"):
                self.delete(job["id"])

    def lighten(self):
        """Older jobs keep their data and small previews; large files (source PDF, plate views,
        full-size PNG, exports) are removed after LIGHT_AFTER_DAYS days."""
        limit = time.time() - self.LIGHT_AFTER_DAYS * 86400
        for summary in self.all():
            if summary["light"] or summary["created"] >= limit or summary["status"] in ("queued", "processing"):
                continue
            folder = self.path(summary["id"])
            (folder / "source.pdf").unlink(missing_ok=True)
            results = folder / "results"
            if results.is_dir():
                for f in results.iterdir():
                    if re.search(r"(\.view-\d+\.(png|jpg)|\.view\.jpg|\.PNG|^export-.*)$", f.name):
                        f.unlink(missing_ok=True)
            self.update(summary["id"], light=True)
