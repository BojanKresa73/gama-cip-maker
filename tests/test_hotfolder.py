"""Hot folder end-to-end, with Serbian Latin and Cyrillic letters in every path and file name."""
import csv
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import gama_cip_desktop.hotfolder as hotfolder
from gama_cip.engine import ghostscript
from gama_cip_desktop.sample_pdf import build
from gama_cip_desktop.server import App


def has_gs():
    try:
        return bool(ghostscript())
    except ValueError:
        return False


def wait(predicate, seconds=120):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.2)
    return False


@unittest.skipUnless(has_gs(), "Ghostscript required")
class HotFolder(unittest.TestCase):
    def setUp(self):
        hotfolder.SCAN_SECONDS = 0.3
        self.tmp = tempfile.TemporaryDirectory(prefix="gama-Đorđe-")
        base = Path(self.tmp.name)
        self.app = App(base / "podaci Šćž")
        self.root = base / "Hot folderi ŠĐČĆŽ šđčćž Ђорђе"
        status = self.app.hot.configure(str(self.root), True)
        self.folders = {f["printshop"]: f for f in status["folders"]}

    def tearDown(self):
        self.app.hot.stop.set()
        self.tmp.cleanup()

    def test_layout_per_printshop(self):
        self.assertEqual(set(self.folders), {"Birograf", "Eurodream"})   # only complete, export-enabled profiles
        self.assertEqual(Path(self.folders["Birograf"]["out"]).name, "XML")
        self.assertEqual(Path(self.folders["Eurodream"]["out"]).name, "PPF")
        for f in self.folders.values():
            for sub in ("Ulaz", "Obradjeno", "Greske"):
                self.assertTrue((Path(f["base"]) / sub).is_dir())
        self.assertTrue((self.root / "PROČITAJ.txt").exists())

    def test_rejects_relative_root(self):
        with self.assertRaises(ValueError):
            self.app.hot.configure("relativni\\folder")

    def test_end_to_end_serbian_names(self):
        bir, eur = Path(self.folders["Birograf"]["base"]), Path(self.folders["Eurodream"]["base"])
        src = Path(self.tmp.name) / "izvor"
        src.mkdir()
        build(src / "b.pdf", 1060, 795)
        build(src / "e.pdf", 745, 605)
        build(src / "pogresan.pdf", 500, 400)

        # 1) Latin with diacritics, copied slowly in chunks like a network copy.
        latin_name = "Kutija štanc Đorđe Čačak 26527.pdf"
        data = (src / "b.pdf").read_bytes()
        target = bir / "Ulaz" / latin_name

        def slow_copy():
            with target.open("wb") as fh:
                for i in range(0, len(data), max(1, len(data) // 8)):
                    fh.write(data[i:i + max(1, len(data) // 8)])
                    fh.flush()
                    time.sleep(0.25)
        copier = threading.Thread(target=slow_copy)
        copier.start()
        # 2) Cyrillic name for the PPF printshop. 3) Wrong format -> Greske.
        (eur / "Ulaz" / "Каталог Ђурђевак 26520.pdf").write_bytes((src / "e.pdf").read_bytes())
        (bir / "Ulaz" / "Pogrešan format.pdf").write_bytes((src / "pogresan.pdf").read_bytes())
        (bir / "Ulaz" / "beleška.txt").write_text("nije PDF", encoding="utf-8")
        copier.join()

        self.assertTrue(wait(lambda: (bir / "Obradjeno" / latin_name).exists()), self.app.hot.status())
        self.assertTrue(wait(lambda: (eur / "Obradjeno" / "Каталог Ђурђевак 26520.pdf").exists()), self.app.hot.status())
        self.assertTrue(wait(lambda: (bir / "Greske" / "Pogrešan format.pdf.greska.txt").exists()), self.app.hot.status())

        xml = sorted(p.name for p in (bir / "XML").iterdir())
        self.assertEqual(xml, ["Kutija_stanc_Djordje_Cacak_26527_0001.PNG", "Kutija_stanc_Djordje_Cacak_26527_0001.xml"])
        text = (bir / "XML" / xml[1]).read_text(encoding="utf-8")
        self.assertIn('JobID="26527"', text)
        self.assertNotIn("ŠTANC", text)   # finishing separation excluded automatically
        self.assertIn("PANTONE 871 C", text)

        ppf = list((eur / "PPF").iterdir())
        self.assertEqual([p.name for p in ppf], ["Katalog_Djurdjevak_26520.p0001.PPF"])
        head = ppf[0].read_bytes()[:400]
        # Cyrillic -> Serbian Latin, written in Windows-1250 like PressPerCent: Đ = \320, đ = \360.
        self.assertIn(b"/CIP3AdmJobName (Katalog \\320ur\\360evak 26520.p0001) def", head)

        reason = (bir / "Greske" / "Pogrešan format.pdf.greska.txt").read_text(encoding="utf-8-sig")
        self.assertIn("profil očekuje", reason)
        self.assertTrue((bir / "Greske" / "Pogrešan format.pdf").exists())
        self.assertTrue((bir / "Ulaz" / "beleška.txt").exists())          # non-PDF left alone
        self.assertEqual([p for p in (bir / "Ulaz").iterdir() if p.suffix == ".pdf"], [])
        self.assertFalse(any(p.name.startswith(".tmp-") for p in (bir / "XML").iterdir()))

        with (self.root / "dnevnik.csv").open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh, delimiter=";"))
        by_file = {r["Fajl"]: r for r in rows}
        self.assertEqual(by_file[latin_name]["Status"], "OK")
        self.assertEqual(by_file["Каталог Ђурђевак 26520.pdf"]["Štamparija"], "Eurodream")
        self.assertEqual(by_file["Pogrešan format.pdf"]["Status"], "GREŠKA")
        self.assertRegex(by_file[latin_name]["Cyan %"], r"^\d+,\d$")

    def test_activity_notifications_and_views(self):
        """The window learns about finished hot-folder jobs, Windows gets a notification, the job has plate views."""
        notes = []
        self.app.notify = lambda title, text: notes.append((title, text))
        eur = Path(self.folders["Eurodream"]["base"])
        build(eur / "izvor.tmp", 745, 605)
        os.replace(eur / "izvor.tmp", eur / "Ulaz" / "Шабац 26520.pdf")
        self.assertTrue(wait(lambda: any(e["status"] == "OK" for e in self.app.hot.activity(0)["events"])), self.app.hot.status())
        done = next(e for e in self.app.hot.activity(0)["events"] if e["status"] == "OK")
        self.assertEqual(done["printshop"], "Eurodream")
        self.assertEqual(done["outputs"], ["Sabac_26520.p0001.PPF"])
        self.assertEqual(Path(done["out_dir"]).name, "PPF")
        self.assertEqual(self.app.hot.activity(done["seq"])["events"], [])
        self.assertEqual(notes[0][0], "Eurodream: gotovo")
        self.assertIn("Шабац 26520.pdf", notes[0][1])
        job = self.app.jobs.get(done["job_id"])
        page = job["result"]["pages"][0]
        for c in page["channels"]:
            self.assertTrue((self.app.jobs.path(job["id"]) / "results" / c["view"]).exists(), c["name"])
        folder = next(f for f in self.app.hot.status()["folders"] if f["printshop"] == "Eurodream")
        self.assertEqual(folder["today_ok"], 1)
        self.assertEqual(folder["last"]["file"], "Шабац 26520.pdf")
        # the activity list survives a restart of the program
        self.app.hot.stop.set()
        again = App(self.app.root)
        try:
            self.assertTrue(any(e["file"] == "Шабац 26520.pdf" and e["status"] == "OK" for e in again.hot.status()["events"]))
        finally:
            again.hot.stop.set()

    def test_resume_after_crash(self):
        """A PDF left in .u_obradi by a closed program is processed again on the next start."""
        bir = Path(self.folders["Birograf"]["base"])
        self.app.hot.stop.set()
        work = bir / "Ulaz" / hotfolder.WORK
        work.mkdir()
        build(work / "Prekinut Ž 31000.pdf", 1060, 795)
        again = App(self.app.root)
        try:
            self.assertTrue(wait(lambda: (bir / "Obradjeno" / "Prekinut Ž 31000.pdf").exists()), again.hot.status())
            self.assertTrue((bir / "XML" / "Prekinut_Z_31000_0001.xml").exists())
        finally:
            again.hot.stop.set()


if __name__ == "__main__":
    unittest.main()
