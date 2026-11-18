"""Rebuild flights/data/airports.tsv from OurAirports (public domain).

    python tools/build_airports.py

Keeps airports with scheduled passenger service and an IATA code.
"""
import csv
import io
import pathlib
import urllib.request

SRC = "https://davidmegginson.github.io/ourairports-data/"
KINDS = {"large_airport": "L", "medium_airport": "M", "small_airport": "S", "seaplane_base": "S"}
REGION_COUNTRIES = {"US", "CA", "AU"}  # show "Seattle, WA" rather than "Seattle, United States"
OUT = pathlib.Path(__file__).resolve().parent.parent / "flights" / "data" / "airports.tsv"


def fetch(name):
    with urllib.request.urlopen(SRC + name, timeout=60) as r:
        return list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))


def clean(text):
    return " ".join(str(text or "").replace("\t", " ").split())


def main():
    countries = {c["code"]: c["name"] for c in fetch("countries.csv")}
    rows = []
    for a in fetch("airports.csv"):
        kind = KINDS.get(a["type"])
        iata = a["iata_code"].strip().upper()
        if not kind or a["scheduled_service"] != "yes" or len(iata) != 3 or not iata.isalnum():
            continue
        cc = a["iso_country"]
        region = a["iso_region"].split("-", 1)[-1] if cc in REGION_COUNTRIES else ""
        rows.append([iata, clean(a["icao_code"] or a["gps_code"]), kind, clean(a["name"]), clean(a["municipality"]),
                     region, clean(countries.get(cc, cc)), f"{float(a['latitude_deg']):.3f}",
                     f"{float(a['longitude_deg']):.3f}", clean(a["keywords"])[:80]])
    rows.sort()
    seen, unique = set(), []
    for r in rows:
        if r[0] not in seen:
            seen.add(r[0])
            unique.append(r)
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="\n") as f:
        f.write("# iata\ticao\tkind\tname\tcity\tregion\tcountry\tlat\tlon\tkeywords  (OurAirports, public domain)\n")
        for r in unique:
            f.write("\t".join(r) + "\n")
    print(f"{len(unique)} airports -> {OUT}")


if __name__ == "__main__":
    main()
