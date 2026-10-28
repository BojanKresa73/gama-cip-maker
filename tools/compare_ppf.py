"""Compare our PPF output with a reference PPF (e.g. from PressPerCent) for the same PDF.

    python tools/compare_ppf.py PROFIL.json TABAK.pdf REFERENCA.PPF

Prints, per separation: ink coverage (reference vs ours), mean pixel difference,
residual offset in pixels and the per-zone coverage difference.
"""
import json
import re
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gama_cip.engine import process_pdf  # noqa: E402
from gama_cip.exporter import ppf_bytes  # noqa: E402
from gama_cip_desktop.store import engine_settings  # noqa: E402


def parse(data):
    head = data[:data.find(b"CIP3BeginSeparation")].decode("latin1")
    names = re.findall(r"\((.*?)\)", re.search(r"SeparationNames \[(.*?)\]", head)[1])
    planes = {}
    pattern = rb"/CIP3PreviewImageWidth (\d+) def\r/CIP3PreviewImageHeight (\d+) def.*?/CIP3PreviewImageDataSize (\d+) def\rCIP3PreviewImage "
    for name, m in zip(names, re.finditer(pattern, data, re.S)):
        w, h, n = map(int, m.groups())
        planes[name] = np.frombuffer(data[m.end():m.end() + n], np.uint8).reshape(h, w)
    return head, planes


def offset(a, b):
    a = 255 - a.astype(float)
    b = 255 - b.astype(float)
    h, w = a.shape
    c = np.fft.irfft2(np.fft.rfft2(a - a.mean(), s=(2 * h, 2 * w)) * np.conj(np.fft.rfft2(b - b.mean(), s=(2 * h, 2 * w))))
    i = np.unravel_index(c.argmax(), c.shape)

    def sub(k, axis):
        idx = list(i)
        idx[axis] = (k - 1) % c.shape[axis]
        left = c[tuple(idx)]
        idx[axis] = (k + 1) % c.shape[axis]
        right = c[tuple(idx)]
        return 0.5 * (left - right) / (left - 2 * c[i] + right)

    dy = (i[0] if i[0] < h else i[0] - 2 * h) + sub(i[0], 0)
    dx = (i[1] if i[1] < w else i[1] - 2 * w) + sub(i[1], 1)
    return dy, dx


def zones(plane, profile):
    ink = 1 - plane.astype(float) / 255
    edges = np.linspace(0, plane.shape[1], profile["zone_count"] + 1).round().astype(int)
    return np.array([ink[:, a:b].mean() for a, b in zip(edges[:-1], edges[1:])])


def main(profile_path, pdf, reference):
    profile = engine_settings(json.loads(Path(profile_path).read_text(encoding="utf-8")))
    profile["ppf_enabled"] = True
    _, ref = parse(Path(reference).read_bytes())
    with tempfile.TemporaryDirectory() as td:
        result = process_pdf(pdf, profile, td)
        page = result["pages"][0]
        _, ours = parse(ppf_bytes(page, profile, page["channels"], td, "x", "Front", "S"))
    print(f"{Path(reference).name}")
    for name, a in ref.items():
        if name not in ours:
            print(f"  {name:12s} NEMA u našem izlazu")
            continue
        b = ours[name]
        dy, dx = offset(a, b)
        zd = (zones(b, profile) - zones(a, profile)) * 100
        print(f"  {name:12s} pokrivenost ref {100 - a.mean() / 2.55:6.2f}%  naš {100 - b.mean() / 2.55:6.2f}%  "
              f"razlika piksela {np.abs(a.astype(float) - b).mean():5.2f}/255  pomak ({dy:+.2f},{dx:+.2f}) px  "
              f"zone: najveća razlika {np.abs(zd).max():.2f} poena")
    for name in ours:
        if name not in ref:
            print(f"  {name:12s} postoji samo kod nas")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
