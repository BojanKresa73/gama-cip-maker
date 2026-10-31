import datetime
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .names import ascii_name, latin

NS = "http://www.CIP4.org/JDFSchema_1_1"
ET.register_namespace("", NS)
PROCESS_CMYK = {"Cyan": "1 0 0 0", "Magenta": "0 1 0 0", "Yellow": "0 0 1 0", "Black": "0 0 0 1"}


def sub(parent, tag, **attrs):
    attrs = {k: str(v) for k, v in attrs.items()}
    if any(re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]", value) for value in attrs.values()):
        raise ValueError("Naziv ili oznaka sadrži znak nedozvoljen u XML-u.")
    return ET.SubElement(parent, "{" + NS + "}" + tag, attrs)


def fmt(value):
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def xml_bytes(page, profile, channels, job_id, side, sheet, png_name):
    """Observed DI-Plot dialect; compatibility with the destination must be checked."""
    root = ET.Element("{" + NS + "}JMF", {"SenderID": "InkZone-Controller", "Version": "1.2"})
    cmd = sub(root, "Command", ID="cmd.01311", Type="Resource")
    params = sub(cmd, "ResourceCmdParams", ResourceName="InkZoneProfile", JobID=job_id)
    ink = sub(params, "InkZoneProfile", ID="r0013", Class="Parameter", Locked="false", Status="Available", PartIDKeys="SignatureName SheetName Side Separation", DescriptiveName="Schieberwerte von DI", ZoneWidth=fmt(profile["zone_width_mm"]))
    sig = sub(ink, "InkZoneProfile", SignatureName=profile["signature"])
    sh = sub(sig, "InkZoneProfile", Locked="false", SheetName=sheet)
    face = sub(sh, "InkZoneProfile", Side=side)
    pool = sub(params, "ColorPool", Class="Parameter", ID="ColorPool", DescriptiveName="Colors for the job", Status="Available")
    spot_index = 0
    for c in channels:
        if len(c["values"]) != profile["zone_count"] or any(not 0 <= v <= 1 for v in c["values"]):
            raise ValueError("Neispravan broj ili opseg vrednosti zona.")
        sub(face, "InkZoneProfile", Separation=c["name"], ZoneSettingsX=" ".join(fmt(v) for v in c["values"]))
        attrs = {"Name": c["name"], "sRGB": " ".join(fmt(v) for v in c["rgb"])}
        if c["name"] in PROCESS_CMYK:
            attrs["CMYK"] = PROCESS_CMYK[c["name"]]
        else:
            spot_index += 1
            attrs.update(ColorBook="PDF alternate colors", ColorBookEntry=str(spot_index), MediaType="Digital")
        sub(pool, "Color", **attrs)
    cmd = sub(root, "Command", ID="cmd.01312", Type="Resource")
    params = sub(cmd, "ResourceCmdParams", ResourceName="Preview", JobID=job_id)
    pre = sub(params, "Preview", ID="r0012.2", Class="Parameter", Status="Available", PartIDKeys="SignatureName SheetName Side", PreviewFileType="PNG", DescriptiveName="png-Files for preview", PreviewUsage="Viewable")
    sig = sub(pre, "Preview", SignatureName=profile["signature"])
    sh = sub(sig, "Preview", SheetName=sheet)
    sub(sh, "Preview", Side=side, URL=profile["preview_url_prefix"] + png_name)
    res = page.get("preview_resolution", profile["preview_dpi"])
    sub(sh, "Preview", Resolution=f"{res} {res}")
    sub(sh, "Preview", Size=" ".join(str(v) for v in page["preview_size"]))
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


PPF_ORDER = ("Black", "Cyan", "Magenta", "Yellow")


def ps_string(text):
    """PostScript string literal; non-ASCII as Windows-1250 octal escapes."""
    out = []
    for byte in latin(text).encode("cp1250", errors="replace"):
        ch = chr(byte)
        if ch in "()\\":
            out.append("\\" + ch)
        elif 32 <= byte < 127:
            out.append(ch)
        else:
            out.append("\\%03o" % byte)
    return "(" + "".join(out) + ")"


def ppf_bytes(page, profile, channels, folder, job_name, side, sheet_name, created=None):
    """CIP3 PPF 2.1 with one 8-bit preview per separation, matching PressPerCent's layout.

    Preview rows are written bottom-up (PostScript image matrix [w 0 0 h 0 0]);
    255 means no ink. Extent is the printable/ink-zone area in points.
    """
    from PIL import Image  # local import keeps XML export light

    if side not in ("Front", "Back"):
        raise ValueError("Strana mora biti Front ili Back.")
    dpi = page["ppf_dpi"]
    w, h = page["ppf_size"]
    width_mm = profile["zone_count"] * profile["zone_width_mm"]
    extent = (width_mm * 72 / 25.4, profile["area_height_mm"] * 72 / 25.4)
    ordered = sorted(channels, key=lambda c: (PPF_ORDER.index(c["name"]) if c["name"] in PPF_ORDER else len(PPF_ORDER), c["name"]))
    now = created or datetime.datetime.now().astimezone()
    offset = now.strftime("%z") or "+0000"
    stamp = now.strftime("D:%Y%m%d%H%M%S") + offset[:3] + "'" + offset[3:] + "'"
    lines = [
        "%!PS-Adobe-3.0", "%%CIP3-File Version 2.1", "CIP3BeginSheet",
        f"/CIP3AdmJobName {ps_string(job_name)} def",
        "/CIP3AdmSoftware (Gama United CIP Maker) def",
        f"/CIP3AdmCreationTime ({stamp}) def",
        f"/CIP3AdmSheetName {ps_string(sheet_name)} def",
        f"/CIP3AdmSheetLay /{profile.get('ppf_sheet_lay', 'Left')} def",
        f"/CIP3AdmPSExtent [ {extent[0]:.6f} {extent[1]:.6f} ] def",
        "/CIP3TransferFilmCurveData [ 0 0 1 1 ] def",
        "/CIP3TransferPlateCurveData [ 0 0 1 1 ] def",
        f"CIP3Begin{side}",
        "/CIP3AdmSeparationNames [" + "".join(ps_string(c["name"]) + " " for c in ordered) + "] def",
        "CIP3BeginPreviewImage",
    ]
    out = bytearray("\r".join(lines).encode("ascii") + b"\r")
    for c in ordered:
        with Image.open(Path(folder) / c["ppf"]) as im:
            if im.mode != "L" or list(im.size) != [w, h]:
                raise ValueError(f"PPF pregled za {c['name']} nema očekivanu veličinu.")
            data = im.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes()
        header = ["CIP3BeginSeparation", f"/CIP3PreviewImageWidth {w} def", f"/CIP3PreviewImageHeight {h} def",
                  "/CIP3PreviewImageBitsPerComp 8 def", "/CIP3PreviewImageComponents 1 def",
                  f"/CIP3PreviewImageMatrix [{w} 0 0 {h} 0 0 ] def", f"/CIP3PreviewImageResolution [{dpi:g} {dpi:g}] def",
                  "/CIP3PreviewImageEncoding /Binary def", "/CIP3PreviewImageCompression /None def",
                  f"/CIP3PreviewImageDataSize {len(data)} def", "CIP3PreviewImage "]
        out += "\r".join(header).encode("ascii") + data + b"\rCIP3EndSeparation\r"
    out += f"CIP3EndPreviewImage\rCIP3End{side}\rCIP3EndSheet\r%%CIP3EndOfFile\r".encode("ascii")
    return bytes(out)


def export_zip(result, folder, selections, job_id, side, source_name):
    if side not in ("Front", "Back") or not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 120:
        raise ValueError("Unesite Job ID i stranu Front/Back.")
    if not isinstance(selections, dict):
        raise ValueError("Nedostaje izbor separacija.")
    root = Path(folder)
    profile = result["profile"]
    # Press consoles get ASCII file names: Serbian Latin/Cyrillic letters are transliterated.
    base = ascii_name(Path(source_name).stem)
    files = []
    exported_pages = []
    for page in result["pages"]:
        selected = selections.get(str(page["page"]), [])
        known = {c["name"] for c in page["channels"]}
        if not isinstance(selected, list) or not selected or not all(isinstance(s, str) and s in known for s in selected):
            raise ValueError(f"Izaberite postojeće separacije za stranu {page['page']}.")
        channels = [c for c in page["channels"] if c["name"] in selected]
        stem = f"{base}_{page['page']:04d}"
        png_name = stem + ".PNG"
        sheet = profile["sheet"] if len(result["pages"]) == 1 else f"{profile['sheet']}_{page['page']}"
        made = []
        if profile.get("xml_enabled", True):
            xml = xml_bytes(page, profile, channels, job_id.strip(), side, sheet, png_name)
            files += [(stem + ".xml", xml), (png_name, root / page["png"])]
            made.append(stem + ".xml")
        if profile.get("ppf_enabled"):
            if not page.get("ppf_size") or not all(c.get("ppf") for c in channels):
                raise ValueError("Ovaj obračun nema PPF preglede. Pokrenite obradu ponovo sa profilom koji ima uključen PPF.")
            ppf_name = f"{base}.p{page['page']:04d}.PPF"
            files.append((ppf_name, ppf_bytes(page, profile, channels, root, f"{Path(source_name).stem}.p{page['page']:04d}", side, f"Sheet {page['page']}")))
            made.append(ppf_name)
        if profile.get("report_enabled"):
            from .report import report_pdf
            pdf_name = f"{base}.p{page['page']:04d}_kljucevi.pdf"
            files.append((pdf_name, report_pdf(page, profile, channels, root, f"{Path(source_name).stem}.p{page['page']:04d}", side, source_name)))
            made.append(pdf_name)
        files.append(("pregled/" + stem + ".png", root / page["thumbnail"]))
        log = root / page.get("render_log", f"page-{page['page']:04d}.render.log")
        if log.exists():
            files.append(("izvestaj/" + log.name, log))
        exported_pages.append({"page": page["page"], "sheet": sheet, "separations": [c["name"] for c in channels], "files": made})
    report = dict(result, export={"job_id": job_id.strip(), "side": side, "source_name": source_name, "pages": exported_pages})
    files.append(("izvestaj/analysis.json", json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")))
    destination = root / "izvoz.zip"
    temporary = root / "izvoz.tmp"
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files:
            if isinstance(data, Path):
                archive.write(data, name)
            else:
                archive.writestr(name, data)
    temporary.replace(destination)
    return destination
