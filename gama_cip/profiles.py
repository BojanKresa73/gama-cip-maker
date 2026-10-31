"""Profiles describe physical geometry; B2 is a label, never a preset geometry."""
import copy
import hashlib
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def validate(p):
    p = copy.deepcopy(p)
    p.setdefault("input_mode", "plate")
    p.setdefault("vertical_anchor", "top")
    p.setdefault("bottom_mm", 0)
    if p.get("schema_version", 1) != 1:
        raise ValueError("Nepodržana verzija formata profila.")
    for key in ("id", "name", "printshop", "machine"):
        if not isinstance(p.get(key), str) or not p[key].strip():
            raise ValueError(f"Nedostaje polje: {key}")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", p["id"]):
        raise ValueError("ID profila sme da sadrži samo slova, brojeve, _ i -.")
    for key in ("plate_width_mm", "plate_height_mm", "zone_width_mm", "area_height_mm"):
        v = p.get(key)
        if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or not 0 < v <= 2500:
            raise ValueError(f"Neispravna dimenzija: {key}")
    for key in ("shift_x_mm", "top_mm", "bottom_mm"):
        v = p.get(key)
        if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or abs(v) > 2500:
            raise ValueError(f"Neispravan pomeraj: {key}")
    if type(p.get("zone_count")) is not int or not 1 <= p["zone_count"] <= 100:
        raise ValueError("Broj zona mora biti ceo broj od 1 do 100.")
    if p["zone_count"] * p["zone_width_mm"] > 2500:
        raise ValueError("Ukupna širina zona je veća od 2500 mm.")
    if type(p.get("rotation")) is not int or p["rotation"] not in (0, 90, 180, 270):
        raise ValueError("Rotacija mora biti 0, 90, 180 ili 270°.")
    for key in ("mirror_x", "mirror_y", "reverse_zones"):
        if type(p.get(key)) is not bool:
            raise ValueError(f"Neispravno logičko polje: {key}")
    if type(p.get("render_dpi")) is not int or p["render_dpi"] not in (300, 600, 1200):
        raise ValueError("Radna rezolucija mora biti 300, 600 ili 1200 dpi.")
    if type(p.get("preview_dpi")) is not int or p["preview_dpi"] not in (25, 50, 72, 75, 150):
        raise ValueError("PNG rezolucija mora biti 25, 50, 72, 75 ili 150 dpi.")
    p.setdefault("preview_scope", "zone")
    p.setdefault("preview_max_px", 0)
    if p["preview_scope"] not in ("zone", "bitmap"):
        raise ValueError("Obuhvat PNG pregleda mora biti „zone“ ili „bitmap“.")
    if type(p["preview_max_px"]) is not int or not (p["preview_max_px"] == 0 or 100 <= p["preview_max_px"] <= 8000):
        raise ValueError("Najveća veličina PNG pregleda mora biti 0 (koristi dpi) ili 100–8000 piksela.")
    for key in ("calc_width_mm", "printable_width_mm"):
        v = p.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or not 0 < v <= 2500):
            raise ValueError(f"Neispravna dimenzija: {key}")
    if p["input_mode"] not in ("plate", "sheet", "any") or p["vertical_anchor"] not in ("top", "bottom", "ink_center", "ink_top"):
        raise ValueError("Neispravan režim ulaznog formata ili vertikalnog poravnanja.")
    if p.get("pdf_box") not in ("MediaBox", "CropBox"):
        raise ValueError("PDF okvir mora biti MediaBox ili CropBox.")
    if p.get("side") not in ("Front", "Back"):
        raise ValueError("Neispravna strana tabaka.")
    for key in ("job_id", "signature", "sheet", "preview_url_prefix", "notes"):
        if not isinstance(p.get(key, ""), str) or len(p.get(key, "")) > 4000:
            raise ValueError(f"Neispravno polje: {key}")
    if p.get("xml_enabled", True) and (not p.get("signature") or not p.get("sheet")):
        raise ValueError("Oznake Signature i Sheet ne smeju biti prazne za XML izlaz.")
    if p.get("validation_status") not in ("reference_compared", "unverified", "press_verified"):
        raise ValueError("Neispravan status profila.")
    p.setdefault("xml_enabled", True)
    p.setdefault("ppf_enabled", False)
    p.setdefault("report_enabled", False)
    if type(p["report_enabled"]) is not bool:
        raise ValueError("Neispravno logičko polje: report_enabled")
    if p["report_enabled"]:
        from .keys import check_scale, curve_points, curve_for
        p.setdefault("key_scale", {"mode": "percent"})
        check_scale(p["key_scale"])
        for name in ("Cyan", "Magenta", "Yellow", "Black", "Spot"):
            curve_points(curve_for(p.get("zone_curves"), name), p["key_scale"])
            feed = curve_for(p.get("feed_curves"), name)
            if feed is not None and not (isinstance(feed, list) and len(feed) >= 2):
                raise ValueError("Kriva dovoda boje mora imati vrednosti za 0 % i 100 %.")
    p.setdefault("ppf_dpi", 50.8)
    p.setdefault("ppf_sheet_lay", "Left")
    for key in ("xml_enabled", "ppf_enabled"):
        if type(p[key]) is not bool:
            raise ValueError(f"Neispravno logičko polje: {key}")
    if p["ppf_dpi"] not in (25.4, 50.8, 101.6):
        raise ValueError("PPF rezolucija mora biti 25,4, 50,8 ili 101,6 dpi.")
    if p["ppf_sheet_lay"] not in ("Left", "Right"):
        raise ValueError("PPF SheetLay mora biti Left ili Right.")
    if not (p["xml_enabled"] or p["ppf_enabled"] or p["report_enabled"]):
        raise ValueError("Uključite bar jedan izlaz: XML, PPF ili PDF sa ključevima.")
    p["schema_version"] = 1
    return p


def fingerprint(p):
    return hashlib.sha256(json.dumps(p, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ProfileStore:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        if not list(self.folder.glob("*.json")):
            for src in (ROOT / "profiles").glob("*.json"):
                (self.folder / src.name).write_bytes(src.read_bytes())

    def all(self):
        return [validate(json.loads(f.read_text(encoding="utf-8"))) for f in sorted(self.folder.glob("*.json"))]

    def get(self, identifier):
        return next((p for p in self.all() if p["id"] == identifier), None)

    def save(self, p):
        p = validate(p)
        old = self.get(p["id"])
        # Any edited or imported profile loses inherited reference validation.
        if not old or p != old:
            p["validation_status"] = "unverified"
        path = self.folder / (p["id"] + ".json")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return p
