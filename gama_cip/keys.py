"""Ink-key values the way PressPerCent shows them to the press operator.

Model (checked against PressPerCent reports of Simbol, 3D Print and ADM Grafika, Oct 2026):

* The key scale is a range of discrete steps.  "Percents" = 0..100; "Custom 1st digit a..b" =
  integers a..b; with a "2nd digit c..d" every 1st-digit value is split into (d-c+1) sub-steps,
  written "4/07".  Scale 2..14 / 1..16 has 207 steps, from 2/01 to 14/16.
* Zone coverage 0..100 % maps linearly onto the whole scale (0 % = first step, 100 % = last step).
* A correction curve (Position -> Value, both on the key scale) is applied; PressPerCent curves
  are splines through the points, with two points a straight line.
* The result is rounded to the nearest step.
* Ink feed: area coverage of the plate inside the printable area (0..100 %) through the
  ink-feed curve, rounded (Percents, precision 1).

Simbol report: mean key step per 1 % coverage = 1.81 for all four plates (model: 181 / 100).
"""
import math
import re

DEFAULT_SCALE = {"mode": "percent"}


def steps(scale):
    """Number of steps minus one (index of the last step)."""
    scale = scale or DEFAULT_SCALE
    if scale.get("mode") == "percent":
        return 100
    a, b = scale["first"]
    if scale.get("second"):
        c, d = scale["second"]
        return (b - a) * (d - c + 1) + (d - c)
    return b - a


def label(index, scale):
    scale = scale or DEFAULT_SCALE
    index = int(index)
    if scale.get("mode") == "percent":
        return str(index)
    a, _ = scale["first"]
    if scale.get("second"):
        c, d = scale["second"]
        n = d - c + 1
        return f"{a + index // n}{scale.get('sep', '/')}{c + index % n:02d}"
    return str(a + index)


def parse(text, scale):
    """'4/07' (or '37', or 37) -> step index on the scale."""
    scale = scale or DEFAULT_SCALE
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        value = float(text)
        if scale.get("mode") == "percent":
            return value
        return value - scale["first"][0]
    text = str(text).strip()
    if scale.get("mode") == "percent":
        return float(text.replace(",", "."))
    a, b = scale["first"]
    if scale.get("second"):
        c, d = scale["second"]
        m = re.fullmatch(r"(\d+)\s*\D\s*(\d+)", text)
        if not m:
            raise ValueError(f"Vrednost „{text}“ nije u obliku {a}{scale.get('sep', '/')}{c:02d}.")
        major, minor = int(m[1]), int(m[2])
        if not (a <= major <= b and c <= minor <= d):
            raise ValueError(f"Vrednost „{text}“ je van skale {a}{scale.get('sep', '/')}{c:02d} – {b}{scale.get('sep', '/')}{d:02d}.")
        return (major - a) * (d - c + 1) + (minor - c)
    value = float(text.replace(",", "."))
    if not a <= value <= b:
        raise ValueError(f"Vrednost {text} je van skale {a}–{b}.")
    return value - a


def check_scale(scale):
    if not isinstance(scale, dict) or scale.get("mode") not in ("percent", "custom"):
        raise ValueError("Skala ključeva mora biti „percent“ ili „custom“.")
    if scale["mode"] == "custom":
        first = scale.get("first")
        if not (isinstance(first, list) and len(first) == 2 and all(type(v) is int for v in first) and 0 <= first[0] < first[1] <= 1000):
            raise ValueError("Skala ključeva: 1. cifra mora biti opseg celih brojeva (npr. 2 do 14).")
        second = scale.get("second")
        if second is not None and not (isinstance(second, list) and len(second) == 2 and all(type(v) is int for v in second) and 0 <= second[0] < second[1] <= 99):
            raise ValueError("Skala ključeva: 2. cifra mora biti opseg celih brojeva (npr. 1 do 16).")
    return scale


def curve_points(curve, scale):
    """Curve as [(position_step, value_step), ...]. A 2-item list [v0, v1] means Position min/max -> v0/v1."""
    last = steps(scale)
    if not curve:
        return [(0.0, 0.0), (float(last), float(last))]
    if len(curve) == 2 and not isinstance(curve[0], (list, tuple)):
        return [(0.0, parse(curve[0], scale)), (float(last), parse(curve[1], scale))]
    points = sorted((parse(p, scale), parse(v, scale)) for p, v in curve)
    if len(points) < 2:
        raise ValueError("Kriva mora imati bar dve tačke.")
    return points


def _spline(points, x):
    """Natural cubic spline through the points (2 points = straight line), clamped at the ends."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    n = len(points)
    if n == 2:
        return ys[0] + (ys[1] - ys[0]) * (x - xs[0]) / (xs[1] - xs[0])
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    alpha = [0.0] + [3 * ((ys[i + 1] - ys[i]) / h[i] - (ys[i] - ys[i - 1]) / h[i - 1]) for i in range(1, n - 1)]
    lo, mu, z = [1.0] + [0.0] * (n - 1), [0.0] * n, [0.0] * n
    for i in range(1, n - 1):
        lo[i] = 2 * (xs[i + 1] - xs[i - 1]) - h[i - 1] * mu[i - 1]
        mu[i] = h[i] / lo[i]
        z[i] = (alpha[i] - h[i - 1] * z[i - 1]) / lo[i]
    c = [0.0] * n
    for j in range(n - 2, -1, -1):
        c[j] = z[j] - mu[j] * c[j + 1]
    i = max(k for k in range(n - 1) if xs[k] <= x)
    b = (ys[i + 1] - ys[i]) / h[i] - h[i] * (c[i + 1] + 2 * c[i]) / 3
    d = (c[i + 1] - c[i]) / (3 * h[i])
    t = x - xs[i]
    return ys[i] + b * t + c[i] * t * t + d * t * t * t


def nearest(x):
    return math.floor(x + 0.5)


def curve_for(curves, name):
    curves = curves or {}
    if name in curves:
        return curves[name]
    if name not in ("Cyan", "Magenta", "Yellow", "Black") and "Spot" in curves:
        return curves["Spot"]
    return curves.get("default")


def zone_keys(values, name, profile):
    """Coverage fractions per zone -> list of key labels."""
    scale = profile.get("key_scale") or DEFAULT_SCALE
    last = steps(scale)
    points = curve_points(curve_for(profile.get("zone_curves"), name), scale)
    out = []
    for v in values:
        step = nearest(_spline(points, min(max(v, 0.0), 1.0) * last))
        out.append(label(min(max(step, 0), last), scale))
    return out


def ink_feed(share, name, profile):
    """Plate coverage inside the printable area (fraction) -> ink feed in % (integer)."""
    curve = curve_for(profile.get("feed_curves"), name) or [0, 100]
    points = [(0.0, float(curve[0])), (100.0, float(curve[1]))] if not isinstance(curve[0], (list, tuple)) else sorted((float(a), float(b)) for a, b in curve)
    return nearest(_spline(points, min(max(share, 0.0), 1.0) * 100))
