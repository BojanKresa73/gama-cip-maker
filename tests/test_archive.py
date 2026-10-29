"""Job archive: fast index, accent/script-insensitive search, filters, cleanup of old large files; side from file name."""
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from gama_cip.names import side_from_name
from gama_cip_desktop.store import JobStore


def fake(root, name, shop, status="completed", days_ago=0, hot=True):
    jid = str(uuid.uuid4())
    folder = Path(root) / "jobs" / jid
    (folder / "results").mkdir(parents=True)
    (folder / "source.pdf").write_bytes(b"%PDF")
    for f in ("page-0001.view.jpg", "page-0001.view-1.jpg", "page-0001.PNG", "export-abc.zip", "page-0001.thumb.png"):
        (folder / "results" / f).write_bytes(b"x")
    job = {"id": jid, "filename": name, "created": time.time() - days_ago * 86400, "status": status,
           "profile": {"id": "p", "name": shop + " profil", "printshop": shop}, "result": None, "hot": {"base": "x"} if hot else None}
    (folder / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    return jid


class Archive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="arhiva-Đorđe-")
        r = self.tmp.name
        self.ids = [fake(r, "Kutija Štanc 26527.pdf", "Birograf"), fake(r, "Каталог Ђурђевак 26520.pdf", "Eurodream"),
                    fake(r, "Letak 31000.pdf", "Birograf", status="failed", hot=False), fake(r, "Stari posao 10000.pdf", "Eurodream", days_ago=120)]
        self.store = JobStore(r)

    def tearDown(self):
        self.tmp.cleanup()

    def test_search_ignores_accents_and_script(self):
        self.assertEqual([j["filename"] for j in self.store.query("stanc")["jobs"]], ["Kutija Štanc 26527.pdf"])
        self.assertEqual([j["filename"] for j in self.store.query("ђурђевак")["jobs"]], ["Каталог Ђурђевак 26520.pdf"])
        self.assertEqual([j["filename"] for j in self.store.query("katalog djurdjevak")["jobs"]], ["Каталог Ђурђевак 26520.pdf"])
        self.assertEqual(self.store.query("26520")["total"], 1)
        self.assertEqual(self.store.query("eurodream")["total"], 2)   # printshop name is searchable too

    def test_filters(self):
        self.assertEqual(self.store.query(printshop="Birograf")["total"], 2)
        self.assertEqual([j["filename"] for j in self.store.query(status="error")["jobs"]], ["Letak 31000.pdf"])
        self.assertEqual(self.store.query(source="manual")["total"], 1)
        self.assertEqual(self.store.query(source="hot")["total"], 3)
        self.assertEqual(self.store.query(days=30)["total"], 3)
        r = self.store.query(limit=2)
        self.assertEqual((len(r["jobs"]), r["total"], r["all"]), (2, 4, 4))
        self.assertEqual(r["printshops"], [["Birograf", 2], ["Eurodream", 2]])

    def test_old_jobs_keep_data_but_lose_large_files(self):
        old = self.store.path(self.ids[3])
        self.assertFalse((old / "source.pdf").exists())
        left = sorted(f.name for f in (old / "results").iterdir())
        self.assertEqual(left, ["page-0001.thumb.png"])
        self.assertTrue(self.store.get(self.ids[3])["light"])
        new = self.store.path(self.ids[0])
        self.assertTrue((new / "source.pdf").exists() and (new / "results" / "page-0001.view.jpg").exists())

    def test_index_survives_restart_and_rebuilds_if_damaged(self):
        self.assertEqual(JobStore(self.tmp.name).query()["total"], 4)
        (Path(self.tmp.name) / "jobs" / "index.json").write_text("pokvareno", encoding="utf-8")
        self.assertEqual(JobStore(self.tmp.name).query("stanc")["total"], 1)

    def test_side_from_file_name(self):
        self.assertIsNone(side_from_name("eurodream 745x605 26527_ed_glasnik.p0002.pdf"))   # PressPerCent: Front
        self.assertEqual(side_from_name("Kutija_back_26527.pdf"), "Back")
        self.assertEqual(side_from_name("Kesa NALIČJE.pdf"), "Back")
        self.assertEqual(side_from_name("Poleđina etikete.pdf"), "Back")
        self.assertIsNone(side_from_name("Feedback forma.pdf"))
        self.assertIsNone(side_from_name("Licenca.pdf"))


if __name__ == "__main__":
    unittest.main()
