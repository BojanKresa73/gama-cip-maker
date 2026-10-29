"""Hot folders: drop a PDF into <root>\\<Štamparija>\\Ulaz, get XML or PPF in <root>\\<Štamparija>\\XML|PPF.

Layout per printshop (one folder per client; a second machine of the same printshop
gets its own sub-folder named after the machine):

    <root>\\Birograf\\Ulaz         PDF goes in here
    <root>\\Birograf\\XML          XML + PNG come out here (PPF for PPF profiles)
    <root>\\Birograf\\Obradjeno    processed PDF is moved here
    <root>\\Birograf\\Greske       failed PDF + "<name>.greska.txt" with the reason
    <root>\\dnevnik.csv            one line per PDF (Excel, ; separated)

Safety rules:
- a PDF is taken only after its size and time stopped changing for two scans AND it
  can be renamed (Windows refuses while another program is still writing it);
- results are written under a temporary name and renamed, so the press never reads
  half a file;
- nothing is produced silently on error: the PDF goes to Greske with an explanation.
"""
import csv
import datetime
import json
import logging
import os
import re
import threading
import time
import zipfile
from collections import deque
from pathlib import Path

from gama_cip.exporter import export_zip
from gama_cip.names import folder_name, job_id_from_name

from .store import blockers

LOG = logging.getLogger("gama.cip.hot")
FINISHING = re.compile(r"(STANC|ŠTANC|BIG|PERFORACIJA)", re.I)
WORK = ".u_obradi"
SCAN_SECONDS = 2.0
CLEAN_SECONDS = 3600          # Obradjeno is cleaned at most once an hour
KEEP_CHOICES = (0, 5, 14, 30, 90)   # days; 0 = never delete


def output_dir_name(profile):
    xml, ppf = profile.get("xml_enabled", True), profile.get("ppf_enabled", False)
    return "Izlaz" if xml and ppf else ("PPF" if ppf else "XML")


def unique(path):
    """'Kutija.pdf' -> 'Kutija (2).pdf' if the name is taken."""
    if not path.exists():
        return path
    for n in range(2, 10000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError("Previše fajlova sa istim imenom.")


def write_atomic(target, data):
    tmp = target.with_name(".tmp-" + target.name)
    tmp.write_bytes(data)
    os.replace(tmp, target)


class HotFolders:
    def __init__(self, app):
        self.app = app
        self.settings_path = Path(app.root) / "settings.json"
        self.lock = threading.RLock()
        self.seen = {}           # path -> (size, mtime)
        self.events_path = Path(app.root) / "hot_events.json"
        self.events = deque(self._load_events(), maxlen=300)
        self.seq = max((e.get("seq", 0) for e in self.events), default=0)
        self.stop = threading.Event()
        self.last_clean = 0
        self.settings = self._load()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="cip-hotfolder")
        self.thread.start()

    # ---------------------------------------------------------------- settings
    def _load(self):
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _load_events(self):
        try:
            return json.loads(self.events_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _save_events(self):
        try:
            tmp = self.events_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self.events), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.events_path)
        except OSError:
            LOG.exception("Evidencija hot foldera nije sačuvana")

    def _save(self):
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.settings_path)

    @property
    def root(self):
        value = self.settings.get("hot_root")
        return Path(value) if value else None

    @property
    def enabled(self):
        return bool(self.root) and self.settings.get("hot_enabled", True)

    @property
    def keep_days(self):
        try:
            return max(0, int(self.settings.get("keep_done_days", 0)))
        except (TypeError, ValueError):
            return 0

    def configure(self, root=None, enabled=None, keep_days=None):
        with self.lock:
            if root is not None:
                root = Path(str(root).strip().strip('"'))
                if not root.is_absolute():
                    raise ValueError("Unesite punu putanju foldera, npr. D:\\CIP Hot folderi.")
                try:
                    root.mkdir(parents=True, exist_ok=True)
                    probe = root / ".proba_šđčćž_ШЂЧЋЖ.tmp"
                    probe.write_text("ok", encoding="utf-8")
                    probe.unlink()
                except OSError as exc:
                    raise ValueError(f"U folder {root} nije moguće upisivati: {exc}") from None
                self.settings["hot_root"] = str(root)
            if enabled is not None:
                self.settings["hot_enabled"] = bool(enabled)
            if keep_days is not None:
                try:
                    keep_days = int(keep_days)
                except (TypeError, ValueError):
                    raise ValueError("Neispravan broj dana.") from None
                if keep_days < 0 or keep_days > 3650:
                    raise ValueError("Broj dana mora biti između 0 i 3650.")
                self.settings["keep_done_days"] = keep_days
                self.last_clean = 0
            self._save()
            if self.enabled:
                self.ensure_folders()
        return self.status()

    # ---------------------------------------------------------------- layout
    def hot_profiles(self):
        out = []
        for p in self.app.profiles.all():
            if p.get("hot_folder", True) and p.get("export_enabled") and not blockers(p):
                out.append(p)
        return out

    def layout(self):
        """{profile_id: {"profile", "base", "in", "out", "done", "errors"}} for every hot profile."""
        root = self.root
        profiles = self.hot_profiles()
        per_shop = {}
        for p in profiles:
            per_shop.setdefault(folder_name(p.get("hot_folder_name") or p["printshop"]).lower(), []).append(p)
        result = {}
        for group in per_shop.values():
            for p in group:
                shop = folder_name(p.get("hot_folder_name") or p["printshop"])
                base = root / shop
                if len(group) > 1:  # several machines for one client: one sub-folder per machine
                    base = base / folder_name(p.get("machine") or p["name"], fallback=p["id"])
                result[p["id"]] = {"profile": p, "base": base, "in": base / "Ulaz", "out": base / output_dir_name(p),
                                   "done": base / "Obradjeno", "errors": base / "Greske"}
        return result

    def ensure_folders(self):
        for item in self.layout().values():
            for key in ("in", "out", "done", "errors"):
                item[key].mkdir(parents=True, exist_ok=True)
        readme = self.root / "PROČITAJ.txt"
        if not readme.exists():
            readme.write_text(
                "Gama United CIP Maker – hot folderi\r\n\r\n"
                "Ubacite PDF u folder Ulaz štamparije. Program pravi XML (ili PPF) u folderu XML/PPF,\r\n"
                "a PDF premešta u Obradjeno. Ako obrada ne uspe, PDF ide u Greske zajedno sa .txt objašnjenjem.\r\n"
                "Broj posla (Job ID) uzima se iz imena fajla: prvi samostalni broj od bar 4 cifre (npr. 26527).\r\n"
                "Uputstvo za rad: u programu, levo u meniju „Uputstvo“.\r\n"
                "Program mora da bude pokrenut (ikonica pored sata).\r\n", encoding="utf-8-sig")

    def status(self):
        layout = self.layout() if self.root else {}
        now = datetime.datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        folders = []
        for item in layout.values():
            pending = 0
            if item["in"].exists():
                pending = sum(1 for f in item["in"].iterdir() if f.is_file() and f.suffix.lower() == ".pdf")
            mine = [e for e in self.events if e.get("profile_id") == item["profile"]["id"]]
            today = [e for e in mine if e["time"] >= midnight]
            last = next((e for e in reversed(mine) if e["status"] != "u obradi"), None)
            folders.append({"profile": item["profile"]["name"], "printshop": item["profile"]["printshop"],
                            "copy_to": (item["profile"].get("hot_copy_to") or "").strip() or None,
                            "profile_id": item["profile"]["id"],
                            "in": str(item["in"]), "out": str(item["out"]), "base": str(item["base"]),
                            "output": output_dir_name(item["profile"]), "pending": pending,
                            "working": sum(1 for e in mine if e["status"] == "u obradi"),
                            "today_ok": sum(1 for e in today if e["status"] == "OK"),
                            "today_errors": sum(1 for e in today if e["status"] == "GREŠKA"), "last": last})
        return {"root": str(self.root) if self.root else None, "enabled": self.enabled, "seq": self.seq,
                "keep_days": self.keep_days, "keep_choices": list(KEEP_CHOICES),
                "folders": sorted(folders, key=lambda f: f["base"].lower()), "events": list(self.events)[::-1][:100]}

    def activity(self, since=0):
        """Events newer than `since` (for the window's live updates)."""
        return {"seq": self.seq, "events": [e for e in self.events if e.get("seq", 0) > since]}

    # ---------------------------------------------------------------- scanning
    def _loop(self):
        recovered = False
        while not self.stop.wait(SCAN_SECONDS):
            if not self.enabled:
                continue
            try:
                layout = self.layout()
                if not recovered:
                    self._recover(layout)
                    recovered = True
                for pid, item in layout.items():
                    self._scan(pid, item)
                if self.keep_days and time.time() - self.last_clean > CLEAN_SECONDS:
                    self.last_clean = time.time()
                    self.clean_done(layout)
            except Exception:  # noqa: BLE001 - the watcher must keep running
                LOG.exception("Greška u praćenju hot foldera")

    def _recover(self, layout):
        """PDFs left in .u_obradi by a closed program go back to Ulaz and are processed again."""
        for item in layout.values():
            work = item["in"] / WORK
            if work.is_dir():
                for f in work.iterdir():
                    if f.is_file():
                        os.replace(f, unique(item["in"] / f.name))
                        LOG.info("Vraćeno u Ulaz posle prekida: %s", f.name)

    def clean_done(self, layout=None):
        """Delete PDFs from Obradjeno older than keep_days (like PressPerCent's "Delete processed jobs after").

        Only *.pdf directly in Obradjeno; age is the time the file was moved there. Returns the count.
        """
        days = self.keep_days
        if not days:
            return 0
        limit = time.time() - days * 86400
        removed = 0
        for item in (layout or self.layout()).values():
            folder = item["done"]
            if not folder.is_dir():
                continue
            for f in folder.iterdir():
                try:
                    if f.is_file() and f.suffix.lower() == ".pdf" and f.stat().st_mtime < limit:
                        f.unlink()
                        removed += 1
                except OSError:
                    LOG.warning("Nije obrisan stari fajl %s", f)
        if removed:
            LOG.info("Obradjeno: obrisano %d PDF-ova starijih od %d dana", removed, days)
        return removed

    def _scan(self, pid, item):
        folder = item["in"]
        if not folder.is_dir():
            folder.mkdir(parents=True, exist_ok=True)
            return
        present = set()
        for f in folder.iterdir():
            present.add(str(f))
        for key in [k for k in self.seen if k.startswith(str(folder) + os.sep) and k not in present]:
            self.seen.pop(key, None)
        for f in folder.iterdir():
            if not f.is_file() or f.name.startswith((".", "~")) or f.suffix.lower() != ".pdf":
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime_ns)
            key = str(f)
            if self.seen.get(key) != sig or st.st_size == 0:
                self.seen[key] = sig   # still arriving (or just seen): look again next scan
                continue
            work = folder / WORK
            work.mkdir(exist_ok=True)
            claimed = unique(work / f.name)
            try:
                os.replace(f, claimed)   # Windows refuses while the copy is still open
            except OSError:
                continue
            self.seen.pop(key, None)
            self._submit(pid, item, claimed, f.name)

    def _submit(self, pid, item, claimed, original):
        try:
            data = claimed.read_bytes()
            job = self.app.submit(original, data, pid, hot={"profile_id": pid, "file": str(claimed), "name": original,
                                                             "base": str(item["base"])})
            self._event(original, item["profile"], "u obradi", "Obrada u toku…", job_id=job["id"])
            LOG.info("Hot folder: %s -> posao %s", original, job["id"])
        except Exception as exc:  # noqa: BLE001
            self._fail(item, claimed, original, str(exc), item["profile"])

    # ---------------------------------------------------------------- results
    def finished(self, job):
        """Called by the worker after every job; handles hot-folder jobs only."""
        hot = job.get("hot")
        if not hot:
            return
        item = self.layout().get(hot["profile_id"])
        base = Path(hot["base"])
        if item is None:  # profile changed after the PDF was taken: keep the original folders
            item = {"base": base, "out": base / output_dir_name(job["profile"]), "done": base / "Obradjeno",
                    "errors": base / "Greske", "profile": job["profile"]}
        claimed, original, profile = Path(hot["file"]), hot["name"], job["profile"]
        if job["status"] != "completed":
            self._fail(item, claimed, original, job.get("error") or "Obrada nije uspela.", profile)
            return
        try:
            made, warnings = self._deliver(job, item)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Izvoz iz hot foldera")
            self._fail(item, claimed, original, f"Izvoz nije uspeo: {exc}", profile, job)
            return
        item["done"].mkdir(parents=True, exist_ok=True)
        done = unique(item["done"] / original)
        os.replace(claimed, done)
        try:
            os.utime(done)   # age in Obradjeno counts from processing, not from when the PDF was made
        except OSError:
            pass
        self._event(original, profile, "OK", ", ".join(made) + ("" if not warnings else " · " + " ".join(warnings)),
                    job_id=job["id"], outputs=made, out_dir=str(item["out"]))
        self._journal(original, profile, job, "OK", made, warnings)
        source = self.app.jobs.path(job["id"]) / "source.pdf"
        source.unlink(missing_ok=True)   # the original lives in Obradjeno

    def _deliver(self, job, item):
        result, profile = job["result"], job["profile"]
        selections, warnings = {}, []
        for page in result["pages"]:
            chosen = [c["name"] for c in page["channels"] if not FINISHING.search(c["name"])]
            skipped = [c["name"] for c in page["channels"] if FINISHING.search(c["name"])]
            if not chosen:
                raise ValueError(f"Strana {page['page']} ima samo separacije dorade ({', '.join(skipped)}).")
            selections[str(page["page"])] = chosen
            if skipped:
                warnings.append(f"Str. {page['page']}: isključeno {', '.join(skipped)}.")
            warnings += [w for w in page.get("warnings", []) if not w.startswith("Moguće separacije dorade")]
        job_id = job_id_from_name(job["filename"])
        side = self.app.side_for(job)
        out = item["out"]
        out.mkdir(parents=True, exist_ok=True)
        made = []
        results = self.app.jobs.path(job["id"]) / "results"
        bundle = export_zip(result, results, selections, job_id, side, job["filename"])
        with zipfile.ZipFile(bundle) as archive:
            for name in archive.namelist():
                if "/" in name:   # pregled/ and izvestaj/ stay in the program
                    continue
                write_atomic(out / name, archive.read(name))
                made.append(name)
        bundle.unlink(missing_ok=True)
        copy_to = (profile.get("hot_copy_to") or "").strip().strip('"')
        if copy_to:   # second destination, e.g. the folder the press console reads (PressPerCent "Folder Action #2")
            try:
                target = Path(copy_to)
                if not target.is_absolute():
                    raise OSError("putanja nije puna (npr. \\\\server\\CIP ili D:\\CIP)")
                target.mkdir(parents=True, exist_ok=True)
                for name in made:
                    write_atomic(target / name, (out / name).read_bytes())
            except OSError as exc:
                LOG.warning("Kopija u %s nije uspela: %s", copy_to, exc)
                warnings.append(f"KOPIJA u {copy_to} NIJE USPELA: {exc}")
        return made, warnings

    def _fail(self, item, claimed, original, reason, profile, job=None):
        LOG.warning("Hot folder greška %s: %s", original, reason)
        try:
            item["errors"].mkdir(parents=True, exist_ok=True)
            target = unique(item["errors"] / original)
            if Path(claimed).exists():
                os.replace(claimed, target)
            stamp = datetime.datetime.now().strftime("%d.%m.%Y. %H:%M")
            (target.with_name(target.name + ".greska.txt")).write_text(
                f"{original}\r\nŠtamparija: {profile.get('printshop')} · Profil: {profile.get('name')}\r\n"
                f"Vreme: {stamp}\r\n\r\nRazlog:\r\n{reason}\r\n\r\n"
                "Ispravite PDF ili profil, pa fajl ponovo ubacite u Ulaz.\r\n", encoding="utf-8-sig")
        except OSError:
            LOG.exception("Premeštanje u Greske nije uspelo")
        self._event(original, profile, "GREŠKA", reason, job_id=(job or {}).get("id") or self._job_for(original),
                    out_dir=str(item["errors"]))
        self._journal(original, profile, job, "GREŠKA", [], [reason])

    def _job_for(self, name):
        return next((e.get("job_id") for e in reversed(self.events) if e["file"] == name and e["status"] == "u obradi"), None)

    def _event(self, name, profile, status, message, job_id=None, outputs=None, out_dir=None):
        with self.lock:
            for old in list(self.events):   # replace the "u obradi" line of the same file with its outcome
                if old["file"] == name and old["status"] == "u obradi" and status != "u obradi":
                    self.events.remove(old)
            self.seq += 1
            self.events.append({"seq": self.seq, "time": time.time(), "file": name, "printshop": profile.get("printshop"),
                                "profile": profile.get("name"), "profile_id": profile.get("id"), "status": status,
                                "message": message[:500], "job_id": job_id, "outputs": outputs or [], "out_dir": out_dir})
            self._save_events()
        if status in ("OK", "GREŠKA") and self.app.notify:
            title = f"{profile.get('printshop')}: {'gotovo' if status == 'OK' else 'GREŠKA'}"
            text = f"{name}\n" + (f"→ {', '.join(o for o in (outputs or []) if not o.endswith('.PNG'))}" if status == "OK" else message[:180])
            if status == "OK" and "NIJE USPELA" in message:
                title = f"{profile.get('printshop')}: gotovo, ali kopija NIJE uspela"
                text = f"{name}\n" + message[message.find("KOPIJA"):][:180]
            try:
                self.app.notify(title, text)
            except Exception:  # noqa: BLE001 - a notification must never break processing
                LOG.exception("Obaveštenje nije prikazano")

    def _journal(self, name, profile, job, status, made, notes):
        """dnevnik.csv: UTF-8 with BOM and ';' so Excel in Serbian locale opens it with the letters intact."""
        if not self.root:
            return
        path = self.root / "dnevnik.csv"
        header = ["Vreme", "Fajl", "Štamparija", "Profil", "Strane", "Status", "Izlaz", "Cyan %", "Magenta %",
                  "Yellow %", "Black %", "Ostale boje", "Napomena"]
        cov, others, pages = {}, set(), 0
        if job and job.get("result"):
            pages = len(job["result"]["pages"])
            for page in job["result"]["pages"]:
                for c in page["channels"]:
                    if c["name"] in ("Cyan", "Magenta", "Yellow", "Black"):
                        cov.setdefault(c["name"], []).append(c["mean"] * 100)
                    else:
                        others.add(c["name"])
        fmt = lambda v: f"{sum(v) / len(v):.1f}".replace(".", ",") if v else ""
        row = [datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S"), name, profile.get("printshop", ""),
               profile.get("name", ""), pages or "", status, " ".join(made)] + \
              [fmt(cov.get(n)) for n in ("Cyan", "Magenta", "Yellow", "Black")] + [", ".join(sorted(others)), " ".join(notes)[:1000]]
        with self.lock:
            new = not path.exists()
            with path.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as fh:
                writer = csv.writer(fh, delimiter=";")
                if new:
                    writer.writerow(header)
                writer.writerow(row)
