"""Ink-key scales and the key-report PDF (PressPerCent "sides" style), DI-Plot placement, factory profiles."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import fitz

from gama_cip import keys
from gama_cip.engine import ghostscript, placement, process_pdf
from gama_cip.exporter import export_zip
from gama_cip_desktop.sample_pdf import build
from gama_cip_desktop.store import ProfileStore, engine_settings, seed_folder

TWO = {"mode": "custom", "first": [2, 14], "second": [1, 16], "sep": "/"}


def seed(pid):
    return json.loads((seed_folder() / f"{pid}.json").read_text(encoding="utf-8"))


def has_gs():
    try:
        return bool(ghostscript())
    except ValueError:
        return False


class Scales(unittest.TestCase):
    def test_two_digit_scale(self):
        self.assertEqual(keys.steps(TWO), 207)
        self.assertEqual(keys.label(0, TWO), "2/01")
        self.assertEqual(keys.label(26, TWO), "3/11")
        self.assertEqual(keys.label(207, TWO), "14/16")
        self.assertEqual(keys.parse("4/07", TWO), 38)
        with self.assertRaises(ValueError):
            keys.parse("15/01", TWO)

    def test_simbol_curve(self):
        p = seed("simbol-745x605")
        # empty zone = 3/11 (curve start), full zone = 14/16; 1 % coverage = 1.81 steps (checked on PressPerCent report)
        self.assertEqual(keys.zone_keys([0, 1, 0.10], "Black", p), ["3/11", "14/16", "4/13"])
        self.assertEqual(keys.ink_feed(0.07172, "Black", p), 9)      # 3D Print report: 7,172 % -> 9
        self.assertEqual(keys.ink_feed(0.14793, "Black", p), 14)     # Simbol report
        self.assertEqual(keys.ink_feed(0.17497, "Magenta", p), 15)

    def test_adm_percent_scale(self):
        p = seed("adm-grafika-724x615")
        self.assertEqual(keys.zone_keys([0, 1], "Cyan", p), ["10", "84"])
        self.assertEqual(keys.zone_keys([0], "Yellow", p), ["15"])   # ADM report: empty Yellow zone = 15
        self.assertEqual(keys.zone_keys([0], "PANTONE 485 C", p), ["10"])
        # ADM report: area coverage -> ink feed 9 / 22 / 14 / 21
        self.assertEqual([keys.ink_feed(v, n, p) for v, n in ((0.06726, "Black"), (0.20844, "Cyan"), (0.12037, "Magenta"), (0.20384, "Yellow"))],
                         [9, 22, 14, 21])

    def test_one_digit_scale_and_spline(self):
        scale = {"mode": "custom", "first": [20, 250]}
        self.assertEqual(keys.steps(scale), 230)
        self.assertEqual(keys.label(0, scale), "20")
        p = {"key_scale": scale, "zone_curves": {"default": [[20, 20], [135, 100], [250, 190]]}}
        out = keys.zone_keys([0, 0.5, 1], "Cyan", p)
        self.assertEqual(out[0], "20")
        self.assertEqual(out[1], "100")
        self.assertEqual(out[2], "190")


class Placement(unittest.TestCase):
    def test_diplot_bitmap_shift(self):
        # DI-Plot: bitmap at bottom centre, BMapShift Y 30 -> PDF bottom 30 mm below the zone-area bottom.
        p = engine_settings(seed("bigraf-745x605"))
        left, top = placement((745, 605), p)
        self.assertAlmostEqual(left, 1.25)
        self.assertAlmostEqual(top, 520 - 605 + 30)
        # Birograf (found from paired samples) follows the same rule: 780 - 795 + 30 = 15
        self.assertEqual(780 - 795 + 30, engine_settings(seed("birograf-rapida105-1060x795"))["top_mm"])


@unittest.skipUnless(has_gs(), "Ghostscript required")
class Report(unittest.TestCase):
    def test_key_report_pdf(self):
        with tempfile.TemporaryDirectory(prefix="izveštaj-Đ-") as td:
            root = Path(td)
            pdf = root / "Kutija Čačak 26527.pdf"
            build(pdf, 745, 605)
            p = engine_settings(seed("simbol-745x605"))
            result = process_pdf(pdf, p, root / "out")
            page = result["pages"][0]
            chosen = [c["name"] for c in page["channels"] if c["name"] != "ŠTANC"]
            bundle = export_zip(result, root / "out", {"1": chosen}, "PPF", "Front", pdf.name)
            with zipfile.ZipFile(bundle) as archive:
                names = [n for n in archive.namelist() if "/" not in n]
                self.assertEqual(names, ["Kutija_Cacak_26527.p0001_kljucevi.pdf"])
                doc = fitz.open(stream=archive.read(names[0]), filetype="pdf")
            text = doc[0].get_text()
            self.assertEqual(doc.page_count, 1)
            self.assertIn("Štamparija: Simbol", text)
            self.assertIn("Kutija Čačak 26527.pdf", text)
            self.assertIn("Dovod boje", text)
            self.assertIn("PANTONE 871 C", text)
            black = next(c for c in page["channels"] if c["name"] == "Black")
            for key in keys.zone_keys(black["values"], "Black", p):
                self.assertIn(key, text)
            self.assertGreater(black["area_coverage"], 0)


class FactoryProfiles(unittest.TestCase):
    def test_sync_into_existing_install(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "profiles"
            folder.mkdir()
            # 1.4 install: incomplete Unagraf exactly as shipped, Eurodream edited by the user.
            (folder / "unagraf-745x605.json").write_text(json.dumps({"id": "unagraf-745x605", "name": "Unagraf · 745 × 605", "printshop": "Unagraf",
                                                                     "machine": None, "active": False}), encoding="utf-8")
            mine = dict(seed("eurodream-745x605"), hot_copy_to="Z:\\CIP", notes="moje")
            (folder / "eurodream-745x605.json").write_text(json.dumps(mine), encoding="utf-8")
            store = ProfileStore(td)
            names = {p["id"]: p for p in store.all()}
            self.assertEqual(len(names), 15)
            self.assertTrue(names["unagraf-745x605"]["usable"])                  # incomplete -> complete factory profile
            self.assertTrue(list((folder / "stari").glob("unagraf-745x605-*.json")))   # old file kept
            self.assertEqual(names["eurodream-745x605"]["hot_copy_to"], "Z:\\CIP")  # user's edit untouched
            self.assertIn("Simbol · SM74 · 745 × 605 · ključevi", store.seed_report["added"])
            # a profile the user deletes does not come back with the next start
            store.delete("simbol-745x605")
            again = ProfileStore(td)
            self.assertNotIn("simbol-745x605", {p["id"] for p in again.all()})
            self.assertEqual(again.seed_report["added"], [])


@unittest.skipUnless(has_gs(), "Ghostscript required")
class PdfBox(unittest.TestCase):
    def test_trimbox_and_fallback(self):
        k = 72 / 25.4
        with tempfile.TemporaryDirectory(prefix="okvir-Š-") as td:
            root = Path(td)
            doc = fitz.open()
            page = doc.new_page(width=800 * k, height=650 * k)
            page.set_trimbox(fitz.Rect(27.5 * k, 22.5 * k, 772.5 * k, 627.5 * k))
            page.draw_rect(fitz.Rect(0, 0, 800 * k, 650 * k), color=None, fill=(0, 0, 0, 1))
            doc.save(root / "trim.pdf")
            plain = fitz.open()
            plain.new_page(width=745 * k, height=605 * k).draw_rect(fitz.Rect(0, 0, 100, 100), color=None, fill=(0, 0, 0))
            plain.save(root / "plain.pdf")
            p = engine_settings(seed("eurodream-745x605"))
            # MediaBox: 800 x 650 is not the 745 x 605 plate
            with self.assertRaises(ValueError):
                process_pdf(root / "trim.pdf", dict(p, pdf_box="MediaBox"), root / "m")
            page = process_pdf(root / "trim.pdf", dict(p, pdf_box="TrimBox"), root / "t")["pages"][0]
            self.assertEqual([round(v, 1) for v in page["source_mm"]], [745.0, 605.0])
            self.assertEqual(page["pdf_box"], "TrimBox")
            black = next(c for c in page["channels"] if c["name"] == "Black")
            self.assertGreater(min(black["values"][1:-1]), 0.99)   # trimmed page fills the zones
            self.assertLess(black["values"][0], 0.97)               # 745 mm page in 747,5 mm of zones: edge zone not full
            page = process_pdf(root / "plain.pdf", dict(p, pdf_box="TrimBox"), root / "p")["pages"][0]
            self.assertEqual(page["pdf_box"], "MediaBox")
            self.assertIn("nema TrimBox", " ".join(page["warnings"]))

    def test_trimbox_larger_than_mediabox(self):
        """Eurodream, Nov 2026: MediaBox = sheet 610 x 430, TrimBox = plate 745 x 605 around it.
        Ghostscript clips the TrimBox to the MediaBox (610 x 430); the sheet must stay where the TrimBox puts it."""
        k = 72 / 25.4
        with tempfile.TemporaryDirectory(prefix="okvir-veci-") as td:
            root = Path(td)
            p = engine_settings(seed("eurodream-745x605"))
            p.update(pdf_box="TrimBox", vertical_anchor="top", top_mm=0)
            for rotate, bbox in ((0, [67.5, 87.5, 367.5, 517.5]), (90, [87.5, 67.5, 517.5, 367.5])):
                doc = fitz.open()
                doc.new_page(width=610 * k, height=430 * k).draw_rect(fitz.Rect(0, 0, 300 * k, 430 * k), color=None, fill=(0, 0, 0, 1))
                doc.save(root / "a.pdf")
                doc = fitz.open(root / "a.pdf")
                doc.xref_set_key(doc[0].xref, "TrimBox", "[%f %f %f %f]" % (-67.5 * k, -87.5 * k, 677.5 * k, 517.5 * k))
                if rotate:
                    doc.xref_set_key(doc[0].xref, "Rotate", str(rotate))
                doc.save(root / f"u{rotate}.pdf")
                page = process_pdf(root / f"u{rotate}.pdf", dict(p, input_mode="any" if rotate else "plate"), root / f"o{rotate}")["pages"][0]
                self.assertEqual(page["pdf_box"], "TrimBox")
                for got, want in zip(page["ink_bbox_mm"], bbox):
                    self.assertAlmostEqual(got, want, delta=0.5)


if __name__ == "__main__":
    unittest.main()
