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
        for profile_id, size in (("birograf-rapida105-1060x795", (1060, 795)), ("eurodream-745x605", (745, 605))):
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
            want = ".xml" if p.get("xml_enabled") else ".PPF"
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
