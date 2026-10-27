"""PDF -> separate 8-bit ink planes -> physical ink coverage. No ink-key calibration."""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote

import fitz
import numpy as np
from PIL import Image

from .profiles import fingerprint, validate

PROCESS = ("Cyan", "Magenta", "Yellow", "Black")
# On Windows, keep Ghostscript from flashing a console window for every page.
NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
PROCESS_RGB = {"Cyan": [0, 1, 1], "Magenta": [1, 0, 1], "Yellow": [1, 1, 0], "Black": [0, 0, 0]}


def ghostscript():
    configured = os.environ.get("GAMA_GS")
    candidates = [configured] if configured else []
    candidates += [shutil.which(n) for n in ("gs", "gswin64c", "gswin32c")]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates += [str(p) for p in sorted((Path(base) / "gs").glob("gs*/bin/gswin*c.exe"), reverse=True)]
    for path in candidates:
        if path and Path(path).is_file():
            return str(Path(path).resolve())
    raise ValueError("Ghostscript nije pronađen. Instalirajte ga ili podesite GAMA_GS na putanju gs/gswin64c.exe.")


def inspect_pdf(path, profile):
    with fitz.open(path) as doc:
        if not doc.is_pdf or doc.needs_pass:
            raise ValueError("Potreban je PDF bez lozinke.")
        if not 1 <= len(doc) <= 200:
            raise ValueError("Podržano je od 1 do 200 strana po PDF-u.")
        pages = []
        for page in doc:
            unit = doc.xref_get_key(page.xref, "UserUnit")
            if unit[0] not in ("null",) and float(unit[1]) != 1:
                raise ValueError("PDF sa nestandardnim UserUnit nije podržan u ovoj verziji.")
            if profile["pdf_box"] == "MediaBox":
                box = page.mediabox
            else:
                box = page.cropbox
            w, h = box.width * 25.4 / 72, box.height * 25.4 / 72
            if page.rotation in (90, 270):
                w, h = h, w
            transformed = (h, w) if profile["rotation"] in (90, 270) else (w, h)
            mismatch = max(abs(transformed[0] - profile["plate_width_mm"]), abs(transformed[1] - profile["plate_height_mm"])) > 0.5
            if profile.get("input_mode", "plate") == "sheet":
                mismatch = transformed[0] > profile["plate_width_mm"] + 0.5 or transformed[1] > profile["plate_height_mm"] + 0.5
            if mismatch:
                raise ValueError(f"Strana {page.number + 1}: format nakon rotacije je {transformed[0]:.2f} × {transformed[1]:.2f} mm, a profil očekuje {profile['plate_width_mm']} × {profile['plate_height_mm']} mm. Izaberite ili podesite odgovarajući profil.")
            pages.append({"page": page.number + 1, "source_mm": [w, h], "oriented_mm": list(transformed), "pdf_rotation": page.rotation})
    return pages


def orient_array(a, p):
    a = np.rot90(a, -(p["rotation"] // 90))
    if p["mirror_x"]:
        a = a[:, ::-1]
    if p["mirror_y"]:
        a = a[::-1]
    return a


def orient_image(image, p):
    turns = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
    if p["rotation"]:
        image = image.transpose(turns[p["rotation"]])
    if p["mirror_x"]:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if p["mirror_y"]:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return image


def placement(mm, p):
    top = p["top_mm"]
    if p.get("vertical_anchor", "top") == "bottom":
        top = p["area_height_mm"] - mm[1] + p.get("bottom_mm", 0)
    return ((p["zone_count"] * p["zone_width_mm"] - mm[0]) / 2 + p["shift_x_mm"], top)


def vertical_integral(a, y):
    y = float(np.clip(y, 0, a.shape[0]))
    n = int(y)
    value = a[:n].sum(axis=0, dtype=np.float64)
    if n < a.shape[0]:
        value += a[n] * (y - n)
    return value


def coverage(a, mm, p):
    """Fractional pixels, blank outside PDF, denominator includes entire zone rectangle."""
    h, w = a.shape
    sx, sy = w / mm[0], h / mm[1]
    left, top = placement(mm, p)
    col = vertical_integral(a, (p["area_height_mm"] - top) * sy) - vertical_integral(a, -top * sy)
    cs = np.r_[0, np.cumsum(col)]
    edges = (np.arange(p["zone_count"] + 1) * p["zone_width_mm"] - left) * sx
    integral = np.interp(edges, np.arange(w + 1), cs, left=0, right=cs[-1])
    values = np.clip(np.diff(integral) / (p["zone_width_mm"] * sx * p["area_height_mm"] * sy), 0, 1)
    if p["reverse_zones"]:
        values = values[::-1]
    return values


def make_preview(image, mm, p, dpi):
    width = p["zone_count"] * p["zone_width_mm"]
    out_size = (round(width * dpi / 25.4), round(p["area_height_mm"] * dpi / 25.4))
    if out_size[0] * out_size[1] > 120_000_000:
        raise ValueError("PNG je prevelik. Smanjite njegovu rezoluciju u profilu.")
    left, top = placement(mm, p)
    # Same physical mapping as coverage; white outside page.
    sx, sy = image.width / mm[0], image.height / mm[1]
    affine = (width / out_size[0] * sx, 0, -left * sx, 0, p["area_height_mm"] / out_size[1] * sy, -top * sy)
    return image.transform(out_size, Image.Transform.AFFINE, affine, resample=Image.Resampling.BICUBIC, fillcolor="white")


def ppf_size(p, dpi):
    """CIP3 preview grid over the printable (ink-zone) area, as PressPerCent writes it."""
    width = p["zone_count"] * p["zone_width_mm"]
    return int(width * dpi / 25.4) + 1, int(p["area_height_mm"] * dpi / 25.4) + 1


def ppf_plane(gray, mm, p, dpi, factor=3):
    """Separation preview on an exact 1/dpi-inch grid (255 = no ink), area-averaged."""
    size = ppf_size(p, dpi)
    hi = (size[0] * factor, size[1] * factor)
    step = 25.4 / (dpi * factor)
    left, top = placement(mm, p)
    sx, sy = gray.width / mm[0], gray.height / mm[1]
    affine = (step * sx, 0, -left * sx, 0, step * sy, -top * sy)
    big = gray.transform(hi, Image.Transform.AFFINE, affine, resample=Image.Resampling.BILINEAR, fillcolor=255)
    return big.resize(size, Image.Resampling.BOX)


def spot_colors(log):
    colors = dict(PROCESS_RGB)
    for name, c, m, y, k in re.findall(r'%%SeparationColor: "(.*?)" 100% ink = (\d+) (\d+) (\d+) (\d+) CMYK', log):
        # Ghostscript uses frac_1=32760 for these tint-transform values.
        c, m, y, k = [min(1, max(0, int(v) / 32760)) for v in (c, m, y, k)]
        colors[name] = [(1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)]
    return colors


def analyze_rasters(folder, source_mm, p, target, stem, progress=lambda s: None, render_log=""):
    folder, target = Path(folder), Path(target)
    target.mkdir(parents=True, exist_ok=True)
    mm = source_mm[::-1] if p["rotation"] in (90, 270) else source_mm
    colors = spot_colors(render_log)
    planes = {}
    composites = []
    for f in folder.glob("*.tif"):
        match = re.search(r"\((.*)\)\.tif$", f.name)
        if match:
            name = unquote(match[1])
            if name in planes:
                raise ValueError("Nejednoznačna imena separacija u rasteru.")
            planes[name] = f
        elif any(f.name.endswith("." + name + ".tif") for name in PROCESS):
            planes[f.name.rsplit(".", 2)[-2]] = f
        else:
            composites.append(f)
    if not planes or len(composites) != 1:
        raise ValueError("Rasterizer nije proizveo očekivane separacije i kompozit.")
    # Never silently rename a spot color due to an OS-escaped output filename.
    reported = set(colors) - set(PROCESS)
    if not reported.issubset(planes):
        raise ValueError("Imena spot boja ne odgovaraju rasterima; potrebno je proveriti specijalne znakove u imenima boja.")
    channels = []
    for n, name in enumerate(sorted(planes, key=lambda x: (PROCESS.index(x) if x in PROCESS else 4, x))):
        progress("Obračun: " + name)
        with Image.open(planes[name]) as im:
            if im.mode != "L":
                raise ValueError("Separacija nije 8-bitni sivi raster.")
            a = orient_array((255 - np.asarray(im)).astype(np.float32) / 255, p)
        values = coverage(a, mm, p)
        if not np.any(values > 0):
            continue
        rgb = colors.get(name, [0.5, 0.5, 0.5])
        # Small separation preview is for viewing; computation above uses full 150 dpi plane.
        preview = Image.fromarray(np.uint8(np.clip((1 - a) * 255, 0, 255)), "L").convert("RGB")
        thumb = make_preview(preview, mm, p, 25)
        thumb_name = f"{stem}.sep-{n + 1}.png"
        thumb.save(target / thumb_name)
        channel = {"name": name, "values": values.tolist(), "mean": float(values.mean()), "max": float(values.max()), "rgb": rgb, "thumbnail": thumb_name}
        if p.get("ppf_enabled"):
            plane = ppf_plane(preview.convert("L"), mm, p, p.get("ppf_dpi", 50.8))
            channel["ppf"] = f"{stem}.ppf-{n + 1}.png"
            plane.save(target / channel["ppf"])
            del plane
        channels.append(channel)
        del a, preview, thumb
    warnings = []
    technical = [c["name"] for c in channels if c["name"].strip().upper() in ("STANC", "ŠTANC", "BIG", "PERFORACIJA")]
    if technical:
        warnings.append("Moguće separacije dorade: " + ", ".join(technical) + ". Proverite da li predstavljaju ploče za štampu pre nego što ih uključite u XML.")
    process_values = {c["name"]: c["values"] for c in channels if c["name"] in PROCESS}
    if all(c in process_values for c in PROCESS[:3]) and np.allclose(process_values["Cyan"], process_values["Magenta"], atol=1e-10) and np.allclose(process_values["Cyan"], process_values["Yellow"], atol=1e-10):
        warnings.append("C, M i Y imaju istu pokrivenost. Mogu poticati samo od registra /All. Proverite koje separacije stvarno idu na ploče i isključite suvišne pre izvoza.")
    if not channels:
        raise ValueError("U izabranom području zona nema boje.")
    with Image.open(composites[0]) as im:
        rgb_image = orient_image(im.convert("RGB"), p)
    preview = make_preview(rgb_image, mm, p, p["preview_dpi"])
    png = stem + ".PNG"
    preview.save(target / png, dpi=(p["preview_dpi"], p["preview_dpi"]))
    size = list(preview.size)
    preview.thumbnail((1600, 1200), Image.Resampling.LANCZOS)
    thumbnail = stem + ".thumb.png"
    preview.save(target / thumbnail)
    page = {"stem": stem, "source_mm": list(source_mm), "oriented_mm": list(mm), "channels": channels, "warnings": warnings, "png": png, "preview_size": size, "thumbnail": thumbnail}
    if p.get("ppf_enabled"):
        page["ppf_dpi"] = p.get("ppf_dpi", 50.8)
        page["ppf_size"] = list(ppf_size(p, page["ppf_dpi"]))
    return page


def process_pdf(pdf, profile, target, progress=lambda s: None):
    p = validate(profile)
    pages = inspect_pdf(pdf, p)
    gs = ghostscript()
    version = subprocess.run([gs, "--version"], capture_output=True, text=True, check=True, timeout=15, **NO_WINDOW).stdout.strip()
    output = Path(target)
    output.mkdir(parents=True, exist_ok=True)
    with open(pdf, "rb") as source:
        input_hash = hashlib.file_digest(source, "sha256").hexdigest()
    result = {"version": "0.1.0", "profile": p, "profile_sha256": fingerprint(p), "input_sha256": input_hash, "renderer": "Ghostscript " + version, "render_dpi": p["render_dpi"], "coverage_dpi": 150, "pages": [], "validation": "Nije provereno učitavanje u štamparsku konzolu; pokrivenost nije kalibracija otvaranja ključeva."}
    for page in pages:
        number = page["page"]
        progress(f"Rasterizovanje strane {number}/{len(pages)}: {p['render_dpi']} → 150 dpi")
        with tempfile.TemporaryDirectory(prefix="gama-raster-") as work:
            args = [gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=tiffsep", f"-r{p['render_dpi']}", f"-dDownScaleFactor={p['render_dpi'] // 150}", "-dOverprint=/enable", "-dPrintSpotCMYK=true", "-dAutoRotatePages=/None", f"-dFirstPage={number}", f"-dLastPage={number}", "-sOutputFile=" + str(Path(work) / "page-%d.tif")]
            if p["pdf_box"] == "CropBox":
                args += ["-dUseCropBox"]
            args.append(str(Path(pdf).resolve()))
            run = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=900, **NO_WINDOW)
            log = run.stdout + "\n" + run.stderr
            (output / f"page-{number:04d}.render.log").write_text(log, encoding="utf-8")
            if run.returncode:
                raise ValueError("Ghostscript nije uspeo da obradi PDF. " + log[-1500:])
            if re.search(r"too many|maximum.*spot|mapped to CMYK|spot.*limit", log, re.I):
                raise ValueError("Rasterizer prijavljuje ograničenje spot separacija. Izvoz je zaustavljen.")
            analyzed = analyze_rasters(work, page["source_mm"], p, output, f"page-{number:04d}", progress, log)
            analyzed.update({"page": number, "pdf_rotation": page["pdf_rotation"]})
            if "warning" in log.lower():
                analyzed["warnings"].append("Rasterizer je izdao upozorenje; pogledajte render.log u izvoznom paketu.")
            result["pages"].append(analyzed)
    (output / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
