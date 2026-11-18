"""Find airports by city, name or code, from the bundled OurAirports list.

Runs entirely offline: typing in the search box never costs an AeroAPI call.
"""
import math
import pathlib
import re
import unicodedata
from functools import lru_cache

DATA = pathlib.Path(__file__).resolve().parent / "data" / "airports.tsv"
SIZE = {"L": 3, "M": 2, "S": 1}
HOME_LARGE_KM = 100  # prefer a big airport this close over a nearer regional one


def fold(text):
    # Punctuation (incl. en dashes, as in "Seattle–Tacoma") becomes a space before accents are stripped.
    text = "".join(c for c in unicodedata.normalize("NFKD", str(text or "")) if not unicodedata.combining(c))
    text = re.sub(r"[^\w]+", " ", text)
    text = text.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).split()


@lru_cache(maxsize=1)
def _airports():
    out = []
    with DATA.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            iata, icao, kind, name, city, region, country, lat, lon, keywords = line.rstrip("\n").split("\t")
            out.append({"code": iata, "icao": icao, "kind": kind, "name": name, "city": city, "region": region,
                        "country": country, "lat": float(lat), "lon": float(lon),
                        "_city": " ".join(fold(city)), "_name": " ".join(fold(name)),
                        "_words": set(fold(" ".join((city, name, region, country, keywords))))})
    return out


@lru_cache(maxsize=1)
def _by_code():
    found = {}
    for a in _airports():
        found.setdefault(a["code"], a)
        if a["icao"]:
            found.setdefault(a["icao"], a)
    return found


def public(a):
    place = ", ".join(p for p in (a["city"], a["region"] or a["country"]) if p)
    return {"code": a["code"], "name": a["name"], "city": a["city"], "place": place}


def get(code):
    """The airport with this IATA or ICAO code, or None."""
    return _by_code().get(str(code or "").strip().upper())


def search(query, limit=8):
    """Airports matching ``query``, best first. Every word must start a word of the
    city, name, region, country or the airport's other names, or be its code."""
    words = fold(query)
    if not words:
        return []
    q = " ".join(words)
    scored = []
    for a in _airports():
        code_hit = len(words) == 1 and q.upper() in (a["code"], a["icao"])
        if not code_hit and not all(any(w2.startswith(w) for w2 in a["_words"]) for w in words):
            continue
        city = a["_city"]
        city_hit = city.startswith(q) or f" {q}" in f" {city}"
        score = (100 if q.upper() == a["code"] else 90 if code_hit else 60 if city_hit
                 else 40 if a["_name"].startswith(q) else 20)
        if all(w in a["_words"] for w in words):  # "kona" -> Kailua-Kona before Konarak
            score += 10
        scored.append((-score, -SIZE[a["kind"]], a["city"], a["code"], a))
    scored.sort(key=lambda s: s[:4])
    return [public(s[-1]) for s in scored[:limit]]


def resolve(text, what):
    """The IATA code for what someone typed: a code, or a city/airport name."""
    tail = re.search(r"\(([A-Za-z0-9]{3,4})\)\s*$", str(text or ""))  # "Seattle (SEA)"
    a = get(tail.group(1) if tail else text)
    if a:
        return a["code"]
    found = search(text, limit=1)
    if found:
        return found[0]["code"]
    raise ValueError(f"Couldn't find a {what} airport called \u201c{str(text).strip()}\u201d."
                     if str(text or "").strip() else f"Enter the {what} city or airport.")


def _km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


@lru_cache(maxsize=4)
def nearest(lat, lon):
    """The airport a hub at (lat, lon) most likely flies from: the closest large
    airport within ``HOME_LARGE_KM``, otherwise the closest large or medium one."""
    if lat is None or lon is None:
        return None
    best = {}
    for a in _airports():
        if a["kind"] in ("L", "M"):
            d = _km(lat, lon, a["lat"], a["lon"])
            for k in {a["kind"], "any"}:
                if k not in best or d < best[k][0]:
                    best[k] = (d, a)
    if not best:
        return None
    if "L" in best and best["L"][0] <= HOME_LARGE_KM:
        return public(best["L"][1])
    return public(best["any"][1])


def home():
    """The hub's nearest airport, or None when the hub's location isn't set."""
    try:
        from bdh_sdk.location import location
        loc = location()
    except Exception:
        return None
    found = nearest(loc.latitude, loc.longitude) if loc else None
    return dict(found) if found else None
