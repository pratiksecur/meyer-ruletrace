"""Unit handling with pint. All comparisons go through here."""

from __future__ import annotations

import re

import pint

ureg = pint.UnitRegistry()
ureg.define("person = [person] = persons")

_ALIASES = {
    "m2": "m**2", "m²": "m**2", "sq m": "m**2", "sqm": "m**2", "m3": "m**3", "m³": "m**3",
    "deg": "degree", "degrees": "degree", "°": "degree", "degree": "degree",
    "min": "minute", "mins": "minute", "minutes": "minute",
    "s": "second", "sec": "second", "secs": "second", "seconds": "second",
    "%": "percent", "per cent": "percent",
    "people": "person", "pax": "person", "passengers": "person",
    "metres": "m", "meters": "m", "metre": "m", "meter": "m",
    "millimetres": "mm", "millimeters": "mm",
    "centimetres": "cm", "centimeters": "cm", "centimetre": "cm", "centimeter": "cm",
    "square metres": "m**2", "square meters": "m**2", "square metre": "m**2", "square meter": "m**2",
    "cubic metres": "m**3", "cubic meters": "m**3", "cubic metre": "m**3", "cubic meter": "m**3",
    "in": "inch", "in.": "inch", "inches": "inch", '"': "inch", "\u2033": "inch",
    "ft": "foot", "feet": "foot", "'": "foot", "\u2032": "foot",
    "ft2": "ft**2", "ft²": "ft**2", "sq ft": "ft**2", "sq. ft.": "ft**2", "square feet": "ft**2", "square foot": "ft**2",
    "ft3": "ft**3", "ft³": "ft**3", "cu ft": "ft**3", "cubic feet": "ft**3", "cubic foot": "ft**3",
}
_ANGLES = {"degree", "radian", "arcminute", "arcsecond"}


class UnitError(ValueError):
    pass


def normalize_unit(u: str) -> str:
    u = (u or "").strip()
    u = re.sub(r"\s+per\s+", "/", u, flags=re.I)
    return "/".join(_ALIASES.get(p.strip(), _ALIASES.get(p.strip().lower(), p.strip())) for p in u.split("/"))


def parse_unit(u: str) -> pint.Unit:
    try:
        return ureg.Unit(normalize_unit(u))
    except Exception as e:  # pint raises several error types
        raise UnitError(f"unknown unit '{u}'") from e


def _is_angle(unit: pint.Unit) -> bool:
    return any(str(name) in _ANGLES for name in unit._units)


def compatible(u1: str, u2: str) -> bool:
    try:
        a, b = parse_unit(u1), parse_unit(u2)
    except UnitError:
        return False
    if _is_angle(a) != _is_angle(b):  # pint treats degrees as dimensionless; keep them apart from % etc.
        return False
    return a.dimensionality == b.dimensionality


def convert(value: float, from_unit: str, to_unit: str) -> float:
    if not compatible(from_unit, to_unit):
        raise UnitError(f"cannot convert {from_unit} to {to_unit}")
    return float(ureg.Quantity(value, parse_unit(from_unit)).to(parse_unit(to_unit)).magnitude)


_FRACTION = re.compile(r"^(?P<whole>\d+(?:\.\d+)?)?\s*(?:(?P<num>\d+)/(?P<den>\d+))?$")
_FT_IN = re.compile(
    r"""^\s*(?P<ft>\d+(?:\.\d+)?)\s*(?:'|\u2032|ft\.?|feet|foot)\s*-?\s*"""
    r"""(?:(?P<inch>\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)\s*(?:"|\u2033|in\.?|inch(?:es)?)?)?\s*$""",
    re.I,
)


def parse_number(s: str) -> float | None:
    """'1.25', '1,25', '39 1/2', '1/2' -> float; anything else -> None."""
    s = (s or "").strip()
    if re.fullmatch(r"-?\d+,\d+", s):
        s = s.replace(",", ".")
    m = _FRACTION.match(s.replace(",", ""))
    if not s or not m or not (m.group("whole") or m.group("num")):
        return None
    v = float(m.group("whole") or 0)
    if m.group("num"):
        v += float(m.group("num")) / float(m.group("den"))
    return v


def parse_value(raw: str, unit: str = "") -> tuple[float | None, str]:
    """Design value as written -> (value, unit). Handles US feet-inch notation:
    3'-6" -> (42.0, "in"), 3 ft 6 in -> (42.0, "in"), 39 1/2 with unit in -> (39.5, "in")."""
    raw = (raw or "").strip()
    unit = {'"': "in", "\u2033": "in", "'": "ft", "\u2032": "ft"}.get((unit or "").strip(), unit)
    v = parse_number(raw)
    if v is not None:
        return v, unit
    m = re.match(r"^\s*(\d+(?:\.\d+)?(?:\s+\d+/\d+)?)\s*(\D+)$", raw)  # value with its unit in the same cell
    if m and parse_number(m.group(1)) is not None:
        try:
            parse_unit(m.group(2))
            u = m.group(2).strip()
            return parse_number(m.group(1)), {'"': "in", "\u2033": "in", "'": "ft", "\u2032": "ft"}.get(u, u)
        except UnitError:
            pass
    m = _FT_IN.match(raw)
    if m and (m.group("inch") or re.search(r"'|\u2032|ft|feet|foot", raw, re.I)):
        inch = parse_number(m.group("inch") or "0") or 0.0
        return float(m.group("ft")) * 12 + inch, "in"
    return None, unit


def fmt(value: float, unit: str) -> str:
    v = f"{value:.6g}"
    return f"{v} {unit}".strip()
