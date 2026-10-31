"""A4 ink-key report for printshops whose operators set the keys by hand (PressPerCent "sides" PDF)."""
import datetime
from pathlib import Path

import fitz

from .keys import ink_feed, zone_keys
from .names import latin

A4 = (595.276, 841.89)
MARGIN = 28
PROCESS_FILL = {"Cyan": (0.80, 0.93, 0.98), "Magenta": (0.98, 0.84, 0.92), "Yellow": (1.0, 0.97, 0.80), "Black": (0.86, 0.86, 0.86)}
UNIT_ORDER = ("Black", "Cyan", "Magenta", "Yellow")


def num(value, digits=3):
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def mm_text(w, h):
    return f"{num(w, 1).rstrip('0').rstrip(',')} × {num(h, 1).rstrip('0').rstrip(',')} mm"


def report_pdf(page, profile, channels, folder, job_name, side, source_name):
    """One A4 page: job data, zone-area picture with zone numbers, and one key row per plate."""
    font = fitz.Font("helv")
    bold = fitz.Font("hebo")
    doc = fitz.open()
    pg = doc.new_page(width=A4[0], height=A4[1])
    writer = fitz.TextWriter(pg.rect)
    black = (0, 0, 0)

    def text(x, y, s, size=8.5, f=None, color=black, writer_=None):
        (writer_ or writer).append((x, y), latin(str(s)), font=f or font, fontsize=size)

    def centred(cx, y, s, size, f=None):
        s = latin(str(s))
        w = (f or font).text_length(s, fontsize=size)
        writer.append((cx - w / 2, y), s, font=f or font, fontsize=size)

    width = A4[0] - 2 * MARGIN
    y = MARGIN + 10
    text(MARGIN, y, f"Posao: {job_name}", 10, bold)
    right = f"Strana tabaka: {side}"
    writer.append((A4[0] - MARGIN - font.text_length(right, fontsize=9), y), right, font=font, fontsize=9)
    pg.draw_line((MARGIN, y + 4), (A4[0] - MARGIN, y + 4), color=(0.7, 0.7, 0.7), width=0.5)
    y += 17
    zone_w = profile["zone_count"] * profile["zone_width_mm"]
    printable_w = profile.get("printable_width_mm") or zone_w
    spots = [c["name"] for c in channels if c["name"] not in UNIT_ORDER]
    lines = [
        f"Štamparija: {profile.get('printshop', '')}    Profil: {profile.get('name', '')}",
        f"Originalni fajl: {source_name}",
        f"Broj ploča: {len(channels)}    Spot boje: {len(spots)}{(' (' + ', '.join(spots) + ')') if spots else ''}",
        f"Ploča: {mm_text(profile['plate_width_mm'], profile['plate_height_mm'])}    "
        f"Štampajuća površina: {mm_text(printable_w, profile['area_height_mm'])}    "
        f"Zone: {profile['zone_count']} × {num(profile['zone_width_mm'], 1).rstrip('0').rstrip(',')} mm",
    ]
    box = page.get("ink_bbox_mm")
    if box:
        lines.append(f"Veličina slike (oblast sa bojom): {mm_text(box[2] - box[0], box[3] - box[1])}")
    for line in lines:
        text(MARGIN, y, line, 8.5)
        y += 12
    y += 4

    # Zone-area picture with zone borders.
    n = profile["zone_count"]
    pic = Path(folder) / page["thumbnail"]
    aspect = profile["area_height_mm"] / zone_w
    rows = len(channels)
    room = A4[1] - y - MARGIN - 16 - rows * 44 - 14
    pic_w = min(width, room / aspect) if aspect > 0 else width
    pic_h = pic_w * aspect
    x0 = MARGIN + (width - pic_w) / 2
    rect = fitz.Rect(x0, y, x0 + pic_w, y + pic_h)
    if pic.exists():
        pg.insert_image(rect, filename=str(pic), keep_proportion=False)
    pg.draw_rect(rect, color=(0.55, 0.55, 0.55), width=0.6)
    zw = pic_w / n
    for i in range(1, n):
        pg.draw_line((x0 + i * zw, y), (x0 + i * zw, y + pic_h), color=(0.2, 0.45, 0.85), width=0.35)
    y += pic_h + 2

    # Zone numbers, then one block per plate with the key values under each zone.
    cell = width / n
    fsize = max(5.0, min(8.5, cell / 3.2))
    reverse = profile.get("reverse_zones")

    def numbers(yy):
        pg.draw_rect(fitz.Rect(MARGIN, yy, MARGIN + width, yy + 11), color=None, fill=(0.93, 0.93, 0.93))
        for i in range(n):
            centred(MARGIN + (i + 0.5) * cell, yy + 8.3, (n - i) if reverse else (i + 1), fsize - 0.5)
        return yy + 13

    y = numbers(y)
    ordered = sorted(channels, key=lambda c: (UNIT_ORDER.index(c["name"]) if c["name"] in UNIT_ORDER else 9, c["name"]))
    units = profile.get("units") or {}
    for k, c in enumerate(ordered):
        fill = PROCESS_FILL.get(c["name"], (0.9, 0.9, 0.95))
        pg.draw_rect(fitz.Rect(MARGIN, y, MARGIN + width, y + 13), color=None, fill=fill)
        unit = units.get(c["name"], k + 1)
        text(MARGIN + 4, y + 9.4, f"Ploča: {c['name']}", 8.5, bold)
        text(MARGIN + 150, y + 9.4, f"Jedinica: {unit}", 8)
        share = c.get("area_coverage", c["mean"])
        info = f"Pokrivenost: {num(share * 100)} %   ({num(c.get('ink_mm2', 0), 1)} mm²)"
        text(MARGIN + 215, y + 9.4, info, 8)
        feed = f"Dovod boje: {ink_feed(share, c['name'], profile)}"
        writer.append((MARGIN + width - 4 - bold.text_length(feed, fontsize=8.5), y + 9.4), feed, font=bold, fontsize=8.5)
        y += 13
        keys = zone_keys(c["values"], c["name"], profile)
        for i, key in enumerate(keys):
            r = fitz.Rect(MARGIN + i * cell, y, MARGIN + (i + 1) * cell, y + 20)
            pg.draw_rect(r, color=(0.45, 0.45, 0.45), width=0.4)
            centred(r.x0 + cell / 2, y + 13.5, key, fsize + 1.5)
        y += 26
    numbers(y - 4)
    foot = (f"Gama United CIP Maker · {datetime.datetime.now().strftime('%d.%m.%Y. %H:%M')} · "
            "Vrednosti ključeva su proračun iz PDF-a; proverite ih na prvom otisku.")
    text(MARGIN, A4[1] - MARGIN + 6, foot, 6.5, color=(0.4, 0.4, 0.4))
    writer.write_text(pg)
    doc.set_metadata({"title": latin(f"{job_name} – ključevi boje"), "creator": "Gama United CIP Maker", "producer": "Gama United CIP Maker"})
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data
