"""CIP3 PPF output, checked against the structure PressPerCent writes for Eurodream."""
import json
import re
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

from gama_cip.engine import ghostscript, process_pdf
from gama_cip.exporter import export_zip, ps_string
from gama_cip_desktop.store import engine_settings
from test_native_integration import vector_pdf

SEED = Path(__file__).resolve().parent.parent / "gama_cip_desktop" / "seed_profiles" / "eurodream-745x605.json"


def has_gs():
    try:
        return bool(ghostscript())
    except ValueError:
        return False


def parse(ppf):
    head = ppf[:ppf.find(b"CIP3BeginSeparation")].decode("ascii")
    planes = {}
    names = re.findall(r"\((.*?)\)", re.search(r"SeparationNames \[(.*?)\]", head)[1])
    for name, m in zip(names, re.finditer(rb"/CIP3PreviewImageWidth (\d+) def\r/CIP3PreviewImageHeight (\d+) def.*?/CIP3PreviewImageDataSize (\d+) def\rCIP3PreviewImage ", ppf, re.S)):
        w, h, n = map(int, m.groups())
        planes[name] = np.frombuffer(ppf[m.end():m.end() + n], np.uint8).reshape(h, w)
        assert ppf[m.end() + n:m.end() + n + 18] == b"\rCIP3EndSeparation"
    return head, planes


class PPF(unittest.TestCase):
    def test_ps_string_escapes(self):
        self.assertEqual(ps_string("a(b)\\c"), "(a\\(b\\)\\\\c)")
        self.assertEqual(ps_string("Š"), "(\\212)")

    @unittest.skipUnless(has_gs(), "Ghostscript required")
    def test_eurodream_layout_matches_presspercent(self):
        p = engine_settings(json.loads(SEED.read_text(encoding="utf-8")))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdf = root / "a.pdf"
            # 745 x 605 mm page: left half black, right half 50% Pantone.
            vector_pdf(pdf)
            data = pdf.read_bytes()
            pts_old = 100 * 72 / 25.4
            data = data.replace(f"/MediaBox [0 0 {pts_old} {pts_old}]".encode(), f"/MediaBox [0 0 {745*72/25.4} {605*72/25.4}]".encode())
            pdf.write_bytes(data)
            result = process_pdf(pdf, p, root / "r")
            page = result["pages"][0]
            self.assertEqual(page["ppf_size"], [1496, 1211])
            names = [c["name"] for c in page["channels"]]
            archive = export_zip(result, root / "r", {"1": names}, "J", "Front", "test.pdf")
            with zipfile.ZipFile(archive) as z:
                self.assertEqual([n for n in z.namelist() if n.endswith(".xml")], [])
                ppf = z.read("test.p0001.PPF")
        head, planes = parse(ppf)
        self.assertIn("/CIP3AdmPSExtent [ 2118.897638 1714.960630 ] def", head)
        self.assertIn("/CIP3AdmSheetLay /Left def", head)
        self.assertIn("CIP3BeginFront", head)
        self.assertTrue(head.startswith("%!PS-Adobe-3.0\r%%CIP3-File Version 2.1\r"))
        self.assertTrue(ppf.endswith(b"CIP3EndPreviewImage\rCIP3EndFront\rCIP3EndSheet\r%%CIP3EndOfFile\r"))
        self.assertEqual(list(planes)[0], "Black")
        # The 100 mm test drawing sits at the bottom-left of the PDF page. Rows are stored
        # bottom-up, so it must appear in the FIRST rows of the file, not the last.
        black = planes["Black"]
        self.assertLess(black[:150, 10:90].mean(), 30)
        self.assertGreater(black[-150:, :].mean(), 250)
        spot = planes["PANTONE 7466C"]
        self.assertAlmostEqual(spot[20:180, 110:180].mean(), 127.5, delta=6)


if __name__ == "__main__":
    unittest.main()
