"""Self-test run by the build pipeline on the finished Windows program.

Proves that the bundled Ghostscript, the engine, XML export and PPF export
all work inside the packaged .exe. Writes a report and exits non-zero on failure.
"""
import json
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path


def run(report_path):
    from gama_cip.engine import ghostscript, process_pdf
    from gama_cip.exporter import export_zip
    from .sample_pdf import build
    from .store import engine_settings, seed_folder

    lines = ["Ghostscript: " + ghostscript()]
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for profile_id, size in (("birograf-rapida105-1060x795", (1060, 795)), ("eurodream-745x605", (745, 605)),
                                 ("simbol-745x605", (745, 605)), ("proof-1030x790", (1030, 790))):
            p = engine_settings(json.loads((seed_folder() / f"{profile_id}.json").read_text(encoding="utf-8")))
            pdf = root / f"{profile_id}.pdf"
            build(pdf, *size)
            result = process_pdf(pdf, p, root / profile_id)
            page = result["pages"][0]
            names = [c["name"] for c in page["channels"]]
            if "ŠTANC" not in names or "PANTONE 871 C" not in names:
                raise AssertionError(f"{profile_id}: neočekivane separacije {names}")
            archive = export_zip(result, root / profile_id, {"1": names}, "TEST", "Front", "selftest.pdf")
            with zipfile.ZipFile(archive) as z:
                files = z.namelist()
            want = ".xml" if p.get("xml_enabled") else ("_kljucevi.pdf" if p.get("report_enabled") else ".PPF")
            if not any(f.endswith(want) for f in files):
                raise AssertionError(f"{profile_id}: nema {want} u izvozu {files}")
            lines.append(f"OK {profile_id}: {', '.join(names)} -> {want}")
    Path(report_path).write_text("\n".join(lines) + "\nSELFTEST OK\n", encoding="utf-8")


def main(report_path):
    try:
        run(report_path)
        return 0
    except Exception:  # noqa: BLE001
        Path(report_path).write_text("SELFTEST FAILED\n" + traceback.format_exc(), encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "selftest.txt"))


def hot_test(root, report, timeout=300):
    """Windows check of hot folders with Serbian Latin and Cyrillic letters in folder and file names."""
    import shutil
    import time
    from .sample_pdf import build
    from .server import App

    lines = []
    try:
        root = Path(root)
        app = App()
        app.hot.configure(str(root), True)
        folders = {f["printshop"]: Path(f["base"]) for f in app.hot.status()["folders"]}
        src = Path(tempfile.mkdtemp(prefix="izvor-Đorđe-"))
        build(src / "b.pdf", 1060, 795)
        build(src / "e.pdf", 745, 605)
        build(src / "x.pdf", 500, 400)
        cases = [
            (folders["Birograf"], "Kutija štanc Đorđe Čačak 26527.pdf", "b.pdf", "XML", "Kutija_stanc_Djordje_Cacak_26527_0001.xml"),
            (folders["Eurodream"], "Каталог Ђурђевак Шабац 26520.pdf", "e.pdf", "PPF", "Katalog_Djurdjevak_Sabac_26520.p0001.PPF"),
            (folders["Tipo štampa"], "Letak Šid 26600.pdf", "t.pdf", "PDF", "Letak_Sid_26600.p0001_kljucevi.pdf"),
        ]
        build(src / "t.pdf", 510, 400)
        for base, name, sample, _, _ in cases:
            shutil.copyfile(src / sample, base / "Ulaz" / name)
        shutil.copyfile(src / "x.pdf", folders["Birograf"] / "Ulaz" / "Pogrešan format ž.pdf")
        end = time.time() + timeout
        def done():
            ok = all((b / out / expected).exists() and (b / "Obradjeno" / n).exists() for b, n, _, out, expected in cases)
            return ok and (folders["Birograf"] / "Greske" / "Pogrešan format ž.pdf.greska.txt").exists()
        while time.time() < end and not done():
            time.sleep(1)
        for base, name, _, out, expected in cases:
            lines.append(f"{'OK' if (base / out / expected).exists() else 'NEMA'} {base.name}/{out}/{expected}")
            lines.append(f"{'OK' if (base / 'Obradjeno' / name).exists() else 'NEMA'} {base.name}/Obradjeno/{name}")
        g = folders["Birograf"] / "Greske" / "Pogrešan format ž.pdf.greska.txt"
        lines.append(f"{'OK' if g.exists() else 'NEMA'} Greske/{g.name}")
        lines.append(f"dnevnik.csv: {'OK' if (root / 'dnevnik.csv').exists() else 'NEMA'}")
        for e in app.hot.status()["events"][:6]:
            lines.append(f"  {e['status']}: {e['file']} {e['message'][:200]}")
        app.hot.stop.set()
        ok = done()
        Path(report).write_text("\n".join(lines) + ("\nHOTTEST OK\n" if ok else "\nHOTTEST FAILED\n"), encoding="utf-8")
        return 0 if ok else 1
    except Exception:  # noqa: BLE001
        Path(report).write_text("\n".join(lines) + "\nHOTTEST FAILED\n" + traceback.format_exc(), encoding="utf-8")
        return 1
