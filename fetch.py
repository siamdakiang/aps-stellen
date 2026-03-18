#!/usr/bin/env python3
"""Fetch APS teacher job postings from Bildungsdirektion OÖ and track changes."""

import json
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
GEO_CACHE = SCRIPT_DIR / "schools_geo.json"

BEZIRKE = {
    1: "Linz-Stadt", 2: "Steyr-Stadt", 3: "Wels-Stadt", 4: "Braunau",
    5: "Eferding", 6: "Freistadt", 7: "Gmunden", 8: "Grieskirchen",
    9: "Kirchdorf", 10: "Linz-Land", 11: "Perg", 12: "Ried",
    13: "Rohrbach", 14: "Schärding", 15: "Steyr-Land", 16: "Urfahr-Umgebung",
    17: "Vöcklabruck", 18: "Wels-Land",
}

BILDUNGSREGION = {
    1: "Linz", 2: "Steyr-Kirchdorf", 3: "Wels-Grieskirchen-Eferding",
    4: "Innviertel", 5: "Wels-Grieskirchen-Eferding", 6: "Mühlviertel",
    7: "Gmunden-Vöcklabruck", 8: "Wels-Grieskirchen-Eferding",
    9: "Steyr-Kirchdorf", 10: "Linz", 11: "Mühlviertel", 12: "Innviertel",
    13: "Mühlviertel", 14: "Innviertel", 15: "Steyr-Kirchdorf",
    16: "Mühlviertel", 17: "Gmunden-Vöcklabruck",
    18: "Wels-Grieskirchen-Eferding",
}

SCHULTYP = {1: "Volksschule", 2: "Mittelschule", 3: "Allgemeine Sonderschule", 4: "Polytechnische Schule"}

EXCEL_EPOCH = datetime(1899, 12, 30)


def load_config():
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def load_chancenbonus():
    with open(SCRIPT_DIR / "chancenbonus.json") as f:
        return set(json.load(f))


def geocode_schools(postings):
    cache = {}
    if GEO_CACHE.exists():
        with open(GEO_CACHE) as f:
            cache = json.load(f)

    unique_schools = {}
    for p in postings:
        skz = p["schulkennzahl"]
        if skz and skz not in unique_schools:
            unique_schools[skz] = p

    new_count = 0
    for skz, p in unique_schools.items():
        if skz in cache:
            continue
        # Build a search query from the school name
        name = p["school_name"]
        # Strip the school type prefix code (e.g. "VS 16 " -> "Sonnensteinschule")
        parts = name.split(",", 1)
        if len(parts) == 2:
            query = f"{parts[1].strip()}, {parts[0].strip()}, Oberösterreich, Austria"
        else:
            query = f"{name}, Oberösterreich, Austria"

        try:
            time.sleep(1)  # Nominatim rate limit: 1 req/sec
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "limit": 1, "countrycodes": "at"},
                headers={"User-Agent": "APS-Stellen-Tracker/1.0"},
                timeout=10,
            )
            results = resp.json()
            if results:
                cache[skz] = {"lat": float(results[0]["lat"]), "lng": float(results[0]["lon"])}
                new_count += 1
                print(f"  Geocoded {skz}: {name} -> {cache[skz]['lat']:.4f}, {cache[skz]['lng']:.4f}")
            else:
                cache[skz] = None
                print(f"  Geocode failed for {skz}: {name}")
        except Exception as e:
            cache[skz] = None
            print(f"  Geocode error for {skz}: {e}")

    if new_count > 0:
        with open(GEO_CACHE, "w") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print(f"Geocoded {new_count} new schools ({sum(1 for v in cache.values() if v)} total with coordinates)")

    return cache


def fetch_xml(url):
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def excel_serial_to_date(serial):
    try:
        return (EXCEL_EPOCH + timedelta(days=int(serial))).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


FACH_CODES = {
    "VL": "Volksschullehrer",
    "VSL": "Volksschullehrer (Sonderpäd.)",
    "ML": "Mittelschullehrer",
    "SL": "Sonderschullehrer",
    "PL": "Polytechnischer Lehrer",
    "RK": "Religion (kath.)",
}


def parse_schulfach(raw):
    """Extract structured data from the SCHULFACH field."""
    # Hours: "VL 22h" or "ML 12h" or "VL 14 h"
    m = re.match(r"([A-Z]{2,3})\s+(\d+)\s*h", raw)
    fach_code = m.group(1) if m else ""
    fach_label = FACH_CODES.get(fach_code, fach_code)
    hours = int(m.group(2)) if m else 0

    # Hour range: "11-22 Wochenstunden" or "15-20 Wochenstunden"
    rm = re.search(r"(\d+)-(\d+)\s*Wochenstunden", raw)
    hours_min = int(rm.group(1)) if rm else hours
    hours_max = int(rm.group(2)) if rm else hours

    # Start date: "ab DD.MM.YYYY" or "ab D.M.YYYY" or "ab sofort"
    dm = re.search(r"ab\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", raw)
    if dm:
        start_date = f"{int(dm.group(3)):04d}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}"
    elif "ab sofort" in raw.lower():
        start_date = "sofort"
    else:
        start_date = ""

    return {
        "fach_code": fach_code,
        "fach_label": fach_label,
        "hours": hours,
        "hours_min": hours_min,
        "hours_max": hours_max,
        "start_date": start_date,
    }


def parse_xml(xml_text, chancenbonus_codes):
    root = ET.fromstring(xml_text)
    postings = []
    for stelle in root.findall("Stelle"):
        dienststelle = (stelle.findtext("DIENSTSTELLE") or "").strip()
        code = dienststelle[:6] if len(dienststelle) >= 6 else ""

        bezirk_code = int(code[1:3]) if len(code) >= 3 and code[1:3].isdigit() else 0
        schultyp_code = int(code[5]) if len(code) >= 6 and code[5].isdigit() else 0

        school_name = dienststelle[7:].strip() if len(dienststelle) > 7 else dienststelle

        befristet_raw = (stelle.findtext("BEFRISTET") or "").strip()
        schulfach_raw = (stelle.findtext("SCHULFACH") or "").strip()
        parsed = parse_schulfach(schulfach_raw)

        postings.append({
            "bezeichnung": (stelle.findtext("BEZEICHNUNG") or "").strip(),
            "dienststelle": dienststelle,
            "schulkennzahl": code,
            "school_name": school_name,
            "schulfach": schulfach_raw,
            **parsed,
            "befristet": befristet_raw,
            "befristet_date": excel_serial_to_date(befristet_raw),
            "bewerber": int((stelle.findtext("BEWERBER") or "0").strip()),
            "schultyp": SCHULTYP.get(schultyp_code, "Unbekannt"),
            "bezirk": BEZIRKE.get(bezirk_code, "Unbekannt"),
            "bildungsregion": BILDUNGSREGION.get(bezirk_code, "Unbekannt"),
            "chancenbonus": code in chancenbonus_codes,
        })
    return postings


def apply_filters(postings, filters):
    result = postings
    if filters.get("schultyp"):
        allowed = set(filters["schultyp"])
        result = [p for p in result if p["schultyp"] in allowed]
    if filters.get("bildungsregion"):
        allowed = set(filters["bildungsregion"])
        result = [p for p in result if p["bildungsregion"] in allowed]
    if filters.get("bezirk"):
        allowed = set(filters["bezirk"])
        result = [p for p in result if p["bezirk"] in allowed]
    if filters.get("chancenbonus_only"):
        result = [p for p in result if p["chancenbonus"]]
    return result


def posting_key(p):
    return f"{p['dienststelle']}|{p['schulfach']}"


def load_previous():
    today = datetime.now().strftime("%Y-%m-%d")
    files = sorted(f for f in DATA_DIR.glob("*.json") if f.stem != today)
    if not files:
        return None
    with open(files[-1]) as f:
        return json.load(f)


def save_snapshot(postings):
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.json"
    with open(path, "w") as f:
        json.dump(postings, f, ensure_ascii=False, indent=2)
    return path


def diff_postings(previous, current):
    prev_keys = {posting_key(p): p for p in previous}
    curr_keys = {posting_key(p): p for p in current}

    added = [curr_keys[k] for k in curr_keys if k not in prev_keys]
    removed = [prev_keys[k] for k in prev_keys if k not in curr_keys]
    return added, removed


def format_posting(p):
    cb = " [CHANCENBONUS]" if p["chancenbonus"] else ""
    return (
        f"  {p['school_name']}{cb}\n"
        f"    Schultyp: {p['schultyp']} | Bezirk: {p['bezirk']} | Region: {p['bildungsregion']}\n"
        f"    Fach: {p['schulfach']}\n"
        f"    Frist: {p['befristet_date'] or 'k.A.'} | Bewerber: {p['bewerber']}"
    )


def send_email(config, added, removed):
    if not config.get("email", {}).get("enabled"):
        return

    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    email_from = os.environ.get("EMAIL_FROM", "")
    email_to = os.environ.get("EMAIL_TO", "")  # comma-separated

    if not all([smtp_host, smtp_user, smtp_password, email_from, email_to]):
        print("Warning: Email enabled but env vars not fully set, skipping.")
        return

    recipients = [addr.strip() for addr in email_to.split(",")]

    lines = [f"APS Stellen Update — {datetime.now().strftime('%Y-%m-%d')}\n"]

    if added:
        lines.append(f"=== {len(added)} NEUE Stellen ===\n")
        for p in added:
            lines.append(format_posting(p))
            lines.append("")

    if removed:
        lines.append(f"=== {len(removed)} ENTFERNTE Stellen ===\n")
        for p in removed:
            lines.append(format_posting(p))
            lines.append("")

    body = "\n".join(lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"APS Stellen: {len(added)} neu, {len(removed)} entfernt — {datetime.now().strftime('%d.%m.%Y')}"
    msg["From"] = email_from
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)

    print(f"Email sent to {', '.join(recipients)}")


def generate_html(postings, geo_cache=None, new_keys=None):
    docs_dir = SCRIPT_DIR / "docs"
    docs_dir.mkdir(exist_ok=True)

    geo_cache = geo_cache or {}
    new_keys = new_keys or set()
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    cb_count = sum(1 for p in postings if p["chancenbonus"])
    new_count = sum(1 for p in postings if posting_key(p) in new_keys)
    zero_applicants = sum(1 for p in postings if p["bewerber"] == 0)

    # Collect unique values for filter chips
    schultypen = sorted(set(p["schultyp"] for p in postings))
    regionen = sorted(set(p["bildungsregion"] for p in postings))
    bezirke = sorted(set(p["bezirk"] for p in postings))

    # Hour range buckets for filter
    HOUR_BUCKETS = [("1-10h", 1, 10), ("11-15h", 11, 15), ("16-20h", 16, 20), ("21-22h", 21, 22)]

    def hour_bucket(h):
        for label, lo, hi in HOUR_BUCKETS:
            if lo <= h <= hi:
                return label
        return ""

    # Build geo JSON for embedding (only entries with coordinates)
    geo_json = json.dumps({k: v for k, v in geo_cache.items() if v}, ensure_ascii=False)

    def iso_to_at(iso_date):
        """Convert YYYY-MM-DD to DD.MM.YYYY."""
        if not iso_date:
            return "k.A."
        try:
            parts = iso_date.split("-")
            return f"{parts[2]}.{parts[1]}.{parts[0]}"
        except (IndexError, ValueError):
            return iso_date

    rows = []
    for p in sorted(postings, key=lambda x: (x["befristet_date"] or "9999-99-99", x["school_name"])):
        cb_badge = ' <span class="badge cb">Chancenbonus</span>' if p["chancenbonus"] else ""
        is_new = posting_key(p) in new_keys
        new_badge = ' <span class="badge new-badge">NEU</span>' if is_new else ""
        skz = p["schulkennzahl"]
        geo = geo_cache.get(skz)
        lat_attr = f' data-lat="{geo["lat"]}"' if geo else ""
        lng_attr = f' data-lng="{geo["lng"]}"' if geo else ""
        school_for_maps = p["school_name"].replace(",", " ") + ", Oberösterreich, Austria"
        maps_url = f"https://www.google.com/maps/dir/?api=1&destination={html_esc(school_for_maps)}"
        iso_date = p["befristet_date"] or ""
        at_date = iso_to_at(p["befristet_date"])
        hours = p.get("hours", 0)
        hbucket = hour_bucket(hours)
        bew = p["bewerber"]
        bew_class = "bew-zero" if bew == 0 else ("bew-low" if bew <= 2 else "")
        hours_display = f'{p.get("hours_min", 0)}-{hours}' if p.get("hours_min", 0) and p.get("hours_min", 0) != hours else str(hours)
        rows.append(
            f'<tr data-schultyp="{html_esc(p["schultyp"])}" '
            f'data-region="{html_esc(p["bildungsregion"])}" '
            f'data-bezirk="{html_esc(p["bezirk"])}" '
            f'data-cb="{1 if p["chancenbonus"] else 0}" '
            f'data-new="{1 if is_new else 0}" '
            f'data-hours="{hours}" '
            f'data-hbucket="{hbucket}" '
            f'data-bew="{bew}" '
            f'data-skz="{html_esc(skz)}"{lat_attr}{lng_attr}>'
            f'<td><span class="school-name">{html_esc(p["school_name"])}</span>{cb_badge}{new_badge}</td>'
            f'<td><span class="badge st-{p["schultyp"][:2].lower()}">{html_esc(p["schultyp"])}</span></td>'
            f'<td>{html_esc(p["bezirk"])}</td>'
            f'<td>{html_esc(p["bildungsregion"])}</td>'
            f'<td class="fach">{html_esc(p["schulfach"])}</td>'
            f'<td class="num">{hours_display}h</td>'
            f'<td data-date="{iso_date}">{html_esc(at_date)}</td>'
            f'<td class="num {bew_class}">{bew}</td>'
            f'<td class="commute-cell" data-minutes="999999">-</td>'
            f'<td class="link-cell">'
            f'<a href="{maps_url}" target="_blank" rel="noopener" title="Route in Google Maps">Route</a>'
            f' &middot; '
            f'<a href="https://bewerbung.bildung.gv.at/app/portal/#/app/bewo" target="_blank" rel="noopener" title="Zum Bewerbungsportal">Bewerben</a>'
            f'</td>'
            f"</tr>"
        )

    def chips(values, group):
        return "".join(
            f'<button class="chip" data-group="{group}" data-value="{html_esc(v)}" onclick="toggleChip(this)">{html_esc(v)}</button>'
            for v in values
        )

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>APS Stellen O\u00d6</title>
<style>
:root {{
  --primary: #1a56db;
  --primary-light: #e1effe;
  --green: #059669;
  --green-light: #d1fae5;
  --amber: #d97706;
  --amber-light: #fef3c7;
  --purple: #7c3aed;
  --purple-light: #ede9fe;
  --rose: #e11d48;
  --rose-light: #ffe4e6;
  --gray-50: #f9fafb;
  --gray-100: #f3f4f6;
  --gray-200: #e5e7eb;
  --gray-300: #d1d5db;
  --gray-500: #6b7280;
  --gray-700: #374151;
  --gray-900: #111827;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--gray-50); color: var(--gray-900); }}
.header {{ background: linear-gradient(135deg, var(--primary) 0%, #1e40af 100%); color: #fff; padding: 2rem 2rem 1.5rem; }}
.header h1 {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 0.25rem; }}
.header .meta {{ color: rgba(255,255,255,0.8); font-size: 0.9rem; }}
.header .stats {{ display: flex; gap: 1.5rem; margin-top: 1rem; }}
.stat {{ background: rgba(255,255,255,0.15); border-radius: 8px; padding: 0.6rem 1rem; }}
.stat .num {{ font-size: 1.4rem; font-weight: 700; }}
.stat .label {{ font-size: 0.75rem; opacity: 0.85; }}
.controls {{ padding: 1rem 2rem; background: #fff; border-bottom: 1px solid var(--gray-200); position: sticky; top: 0; z-index: 10; }}
.search-row {{ display: flex; gap: 0.75rem; align-items: center; margin-bottom: 0.75rem; flex-wrap: wrap; }}
.search-row input {{ flex: 1; min-width: 200px; max-width: 400px; padding: 0.5rem 0.75rem; font-size: 0.9rem; border: 1px solid var(--gray-300); border-radius: 6px; outline: none; }}
.search-row input:focus {{ border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-light); }}
.reset-btn, .commute-btn {{ padding: 0.5rem 1rem; font-size: 0.85rem; border: 1px solid var(--gray-300); border-radius: 6px; background: #fff; cursor: pointer; color: var(--gray-700); }}
.reset-btn:hover, .commute-btn:hover {{ background: var(--gray-100); }}
.commute-btn {{ border-color: var(--primary); color: var(--primary); font-weight: 600; }}
.commute-btn:hover {{ background: var(--primary-light); }}
.commute-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
.commute-row {{ display: flex; gap: 0.75rem; align-items: center; margin-bottom: 0.75rem; flex-wrap: wrap; }}
.commute-row input {{ flex: 1; min-width: 200px; max-width: 400px; padding: 0.5rem 0.75rem; font-size: 0.9rem; border: 1px solid var(--gray-300); border-radius: 6px; outline: none; }}
.commute-row input:focus {{ border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-light); }}
.commute-status {{ font-size: 0.8rem; color: var(--gray-500); }}
.filter-group {{ margin-bottom: 0.5rem; display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem; }}
.filter-label {{ font-size: 0.75rem; font-weight: 600; color: var(--gray-500); text-transform: uppercase; letter-spacing: 0.05em; min-width: 100px; }}
.chip {{ padding: 0.3rem 0.7rem; font-size: 0.8rem; border-radius: 99px; border: 1px solid var(--gray-300); background: #fff; cursor: pointer; transition: all 0.15s; color: var(--gray-700); }}
.chip:hover {{ background: var(--gray-100); }}
.chip.active {{ border-color: var(--primary); background: var(--primary-light); color: var(--primary); font-weight: 600; }}
.chip.active[data-group="cb"] {{ border-color: var(--green); background: var(--green-light); color: var(--green); }}
.count {{ font-size: 0.85rem; color: var(--gray-500); padding: 0.75rem 2rem 0.5rem; }}
.table-wrap {{ padding: 0 2rem 2rem; overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
th {{ background: var(--gray-700); color: #fff; padding: 0.6rem 0.75rem; text-align: left; font-size: 0.8rem; font-weight: 600; cursor: pointer; white-space: nowrap; user-select: none; position: sticky; top: 0; }}
th:hover {{ background: var(--gray-500); }}
th.sortable::after {{ content: " \u2195"; opacity: 0.4; font-size: 0.7rem; }}
th.no-sort {{ cursor: default; }}
th.no-sort:hover {{ background: var(--gray-700); }}
td {{ padding: 0.55rem 0.75rem; border-bottom: 1px solid var(--gray-100); font-size: 0.85rem; vertical-align: top; }}
tr:hover td {{ background: #f0f5ff; }}
tr.hidden {{ display: none; }}
.school-name {{ font-weight: 500; }}
.fach {{ max-width: 350px; color: var(--gray-700); }}
.num {{ text-align: center; font-weight: 600; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 99px; font-size: 0.7rem; font-weight: 600; vertical-align: middle; margin-left: 0.4rem; }}
.badge.cb {{ background: var(--green-light); color: var(--green); }}
.badge.st-vo {{ background: var(--primary-light); color: var(--primary); }}
.badge.st-mi {{ background: var(--purple-light); color: var(--purple); }}
.badge.st-al {{ background: var(--amber-light); color: var(--amber); }}
.badge.st-po {{ background: var(--rose-light); color: var(--rose); }}
.commute-cell {{ text-align: center; white-space: nowrap; }}
.commute-short {{ color: var(--green); font-weight: 600; }}
.commute-medium {{ color: var(--amber); font-weight: 600; }}
.commute-long {{ color: var(--rose); font-weight: 600; }}
.link-cell {{ white-space: nowrap; }}
.link-cell a {{ color: var(--primary); text-decoration: none; font-size: 0.8rem; }}
.link-cell a:hover {{ text-decoration: underline; }}
.footer {{ padding: 1.5rem 2rem; text-align: center; font-size: 0.75rem; color: var(--gray-500); border-top: 1px solid var(--gray-200); }}
.footer a {{ color: var(--gray-500); text-decoration: none; }}
.footer a:hover {{ color: var(--primary); text-decoration: underline; }}
.footer span {{ margin: 0 0.5rem; }}
.badge.new-badge {{ background: #fef3c7; color: #92400e; animation: pulse 2s ease-in-out 3; }}
@keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}
.bew-zero {{ color: var(--green); }}
.bew-low {{ color: var(--amber); }}
@media (max-width: 768px) {{
  .header, .controls, .table-wrap, .count {{ padding-left: 1rem; padding-right: 1rem; }}
  .filter-label {{ min-width: auto; width: 100%; }}
  .header .stats {{ flex-wrap: wrap; gap: 0.75rem; }}
}}
</style>
</head>
<body>
<div class="header">
  <h1>Offene APS-Stellen Ober\u00f6sterreich</h1>
  <div class="meta">Stellenausschreibungen f\u00fcr Landeslehrer im Pflichtschulbereich &mdash; Stand: {now}</div>
  <div class="stats">
    <div class="stat"><div class="num">{len(postings)}</div><div class="label">Offene Stellen</div></div>
    <div class="stat"><div class="num">{new_count}</div><div class="label">Neu heute</div></div>
    <div class="stat"><div class="num">{zero_applicants}</div><div class="label">Ohne Bewerber</div></div>
    <div class="stat"><div class="num">{cb_count}</div><div class="label">Chancenbonus</div></div>
  </div>
</div>
<div class="controls">
  <div class="search-row">
    <input type="text" id="q" placeholder="Suche (Schule, Fach, Ort...)" oninput="applyFilters()">
    <button class="reset-btn" onclick="resetAll()">Zur\u00fccksetzen</button>
  </div>
  <div class="commute-row">
    <input type="text" id="address" placeholder="Ihre Adresse eingeben (z.B. Hauptplatz 1, Linz)" onkeydown="if(event.key==='Enter')calcCommute()">
    <button class="commute-btn" id="commuteBtn" onclick="calcCommute()">Anfahrt berechnen</button>
    <span class="commute-status" id="commuteStatus"></span>
  </div>
  <div class="filter-group">
    <span class="filter-label">Schultyp</span>
    {chips(schultypen, "schultyp")}
  </div>
  <div class="filter-group">
    <span class="filter-label">Region</span>
    {chips(regionen, "region")}
  </div>
  <div class="filter-group">
    <span class="filter-label">Bezirk</span>
    {chips(bezirke, "bezirk")}
  </div>
  <div class="filter-group">
    <span class="filter-label">Stunden</span>
    <button class="chip" data-group="hbucket" data-value="1-10h" onclick="toggleChip(this)">1-10h</button>
    <button class="chip" data-group="hbucket" data-value="11-15h" onclick="toggleChip(this)">11-15h</button>
    <button class="chip" data-group="hbucket" data-value="16-20h" onclick="toggleChip(this)">16-20h</button>
    <button class="chip" data-group="hbucket" data-value="21-22h" onclick="toggleChip(this)">21-22h</button>
  </div>
  <div class="filter-group">
    <span class="filter-label">Sonstiges</span>
    <button class="chip" data-group="cb" data-value="1" onclick="toggleChip(this)">Nur Chancenbonus</button>
    <button class="chip" data-group="new" data-value="1" onclick="toggleChip(this)">Nur neue Stellen</button>
    <button class="chip" data-group="nobew" data-value="1" onclick="toggleChip(this)">Ohne Bewerber</button>
  </div>
</div>
<div class="count" id="count">{len(postings)} Stellen angezeigt</div>
<div class="table-wrap">
<table id="stellen">
<thead><tr>
<th class="sortable" onclick="sortTable(0)">Schule</th>
<th class="sortable" onclick="sortTable(1)">Schultyp</th>
<th class="sortable" onclick="sortTable(2)">Bezirk</th>
<th class="sortable" onclick="sortTable(3)">Region</th>
<th class="sortable" onclick="sortTable(4)">Fach / Details</th>
<th class="sortable" onclick="sortTable(5)">Stunden</th>
<th class="sortable" onclick="sortTable(6)">Frist</th>
<th class="sortable" onclick="sortTable(7)">Bewerber</th>
<th class="sortable" onclick="sortTable(8)">Anfahrt</th>
<th class="no-sort">Links</th>
</tr></thead>
<tbody>
{"".join(rows)}
</tbody>
</table>
</div>
<script>
const GEO = {geo_json};
const filters = {{ schultyp: new Set(), region: new Set(), bezirk: new Set(), hbucket: new Set(), cb: new Set(), "new": new Set(), nobew: new Set() }};

function toggleChip(el) {{
  const g = el.dataset.group, v = el.dataset.value;
  if (filters[g].has(v)) {{ filters[g].delete(v); el.classList.remove("active"); }}
  else {{ filters[g].add(v); el.classList.add("active"); }}
  applyFilters();
}}

function applyFilters() {{
  const q = document.getElementById("q").value.toLowerCase();
  let shown = 0;
  document.querySelectorAll("#stellen tbody tr").forEach(r => {{
    let vis = true;
    if (q && !r.textContent.toLowerCase().includes(q)) vis = false;
    if (vis && filters.schultyp.size && !filters.schultyp.has(r.dataset.schultyp)) vis = false;
    if (vis && filters.region.size && !filters.region.has(r.dataset.region)) vis = false;
    if (vis && filters.bezirk.size && !filters.bezirk.has(r.dataset.bezirk)) vis = false;
    if (vis && filters.hbucket.size && !filters.hbucket.has(r.dataset.hbucket)) vis = false;
    if (vis && filters.cb.size && r.dataset.cb !== "1") vis = false;
    if (vis && filters["new"].size && r.dataset.new !== "1") vis = false;
    if (vis && filters.nobew.size && r.dataset.bew !== "0") vis = false;
    r.classList.toggle("hidden", !vis);
    if (vis) shown++;
  }});
  document.getElementById("count").textContent = shown + " Stellen angezeigt";
}}

function resetAll() {{
  document.getElementById("q").value = "";
  for (const g in filters) filters[g].clear();
  document.querySelectorAll(".chip.active").forEach(c => c.classList.remove("active"));
  document.querySelectorAll(".commute-cell").forEach(c => {{ c.textContent = "-"; c.className = "commute-cell"; c.dataset.minutes = "999999"; }});
  document.getElementById("address").value = "";
  document.getElementById("commuteStatus").textContent = "";
  applyFilters();
}}

let sortDir = {{}};
function sortTable(col) {{
  const tb = document.querySelector("#stellen tbody");
  const rows = Array.from(tb.rows);
  sortDir[col] = !sortDir[col];
  rows.sort((a, b) => {{
    if (col === 8) {{
      const am = parseFloat(a.cells[8].dataset.minutes) || 999999;
      const bm = parseFloat(b.cells[8].dataset.minutes) || 999999;
      return sortDir[col] ? am - bm : bm - am;
    }}
    if (col === 6) {{
      const ad = a.cells[6].dataset.date || "9999-99-99";
      const bd = b.cells[6].dataset.date || "9999-99-99";
      return sortDir[col] ? ad.localeCompare(bd) : bd.localeCompare(ad);
    }}
    if (col === 5 || col === 7) {{
      const an = parseFloat(a.cells[col].textContent) || 0;
      const bn = parseFloat(b.cells[col].textContent) || 0;
      return sortDir[col] ? an - bn : bn - an;
    }}
    let x = a.cells[col].textContent, y = b.cells[col].textContent;
    return sortDir[col] ? x.localeCompare(y, "de") : y.localeCompare(x, "de");
  }});
  rows.forEach(r => tb.appendChild(r));
}}

async function calcCommute() {{
  const addr = document.getElementById("address").value.trim();
  if (!addr) return;
  const btn = document.getElementById("commuteBtn");
  const status = document.getElementById("commuteStatus");
  btn.disabled = true;
  status.textContent = "Adresse wird gesucht...";

  try {{
    // Geocode user address via Nominatim
    const geoResp = await fetch(
      "https://nominatim.openstreetmap.org/search?" + new URLSearchParams({{
        q: addr, format: "json", limit: "1", countrycodes: "at"
      }}), {{ headers: {{ "User-Agent": "APS-Stellen-Tracker/1.0" }} }}
    );
    const geoData = await geoResp.json();
    if (!geoData.length) {{
      status.textContent = "Adresse nicht gefunden. Bitte genauer eingeben.";
      btn.disabled = false;
      return;
    }}
    const userLat = parseFloat(geoData[0].lat);
    const userLng = parseFloat(geoData[0].lon);
    status.textContent = "Fahrzeiten werden berechnet...";

    // Collect all unique school coordinates
    const rows = document.querySelectorAll("#stellen tbody tr");
    const schoolCoords = [];
    const skzToIdx = {{}};
    rows.forEach(r => {{
      const skz = r.dataset.skz;
      if (skz && GEO[skz] && !(skz in skzToIdx)) {{
        skzToIdx[skz] = schoolCoords.length;
        schoolCoords.push(GEO[skz]);
      }}
    }});

    if (!schoolCoords.length) {{
      status.textContent = "Keine Schulkoordinaten verfügbar.";
      btn.disabled = false;
      return;
    }}

    // Build OSRM table request: user as source, all schools as destinations
    const coords = [[userLng, userLat], ...schoolCoords.map(c => [c.lng, c.lat])];
    const coordStr = coords.map(c => c[0] + "," + c[1]).join(";");
    const destIndices = schoolCoords.map((_, i) => i + 1).join(";");
    const osrmUrl = "https://router.project-osrm.org/table/v1/driving/" + coordStr
      + "?sources=0&destinations=" + destIndices + "&annotations=duration";

    const osrmResp = await fetch(osrmUrl);
    const osrmData = await osrmResp.json();

    if (osrmData.code !== "Ok") {{
      status.textContent = "Routenberechnung fehlgeschlagen. Bitte erneut versuchen.";
      btn.disabled = false;
      return;
    }}

    const durations = osrmData.durations[0]; // seconds from user to each school

    // Update table cells
    rows.forEach(r => {{
      const skz = r.dataset.skz;
      const cell = r.cells[8];
      if (skz && skz in skzToIdx) {{
        const secs = durations[skzToIdx[skz]];
        if (secs !== null) {{
          const mins = Math.round(secs / 60);
          cell.textContent = mins + " min";
          cell.dataset.minutes = mins;
          if (mins <= 20) cell.className = "commute-cell commute-short";
          else if (mins <= 45) cell.className = "commute-cell commute-medium";
          else cell.className = "commute-cell commute-long";
        }} else {{
          cell.textContent = "k.A.";
          cell.dataset.minutes = "999999";
        }}
      }} else {{
        cell.textContent = "k.A.";
        cell.dataset.minutes = "999999";
      }}
    }});

    // Update Google Maps links with user address as origin
    const origin = encodeURIComponent(addr);
    rows.forEach(r => {{
      const links = r.cells[9];
      const routeLink = links.querySelector("a");
      if (routeLink) {{
        routeLink.href = routeLink.href + "&origin=" + origin;
      }}
    }});

    status.textContent = "Fahrzeiten berechnet (Auto, via OSRM)";

    // Auto-sort by commute time
    sortDir[8] = false;
    sortTable(8);

  }} catch(e) {{
    status.textContent = "Fehler: " + e.message;
  }}
  btn.disabled = false;
}}
</script>
<div class="footer">
  <a href="https://github.com/siamdakiang/aps-stellen" target="_blank" rel="noopener">GitHub</a>
  <span>&middot;</span>
  Built with <a href="https://claude.ai/claude-code" target="_blank" rel="noopener">Claude Code</a>
  <span>&middot;</span>
  ~500k tokens spent
</div>
</body>
</html>"""

    path = docs_dir / "index.html"
    with open(path, "w") as f:
        f.write(html)
    print(f"HTML page generated at {path}")


def html_esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def main():
    config = load_config()
    chancenbonus_codes = load_chancenbonus()

    print(f"Fetching APS postings from {config['url']}...")
    xml_text = fetch_xml(config["url"])

    all_postings = parse_xml(xml_text, chancenbonus_codes)
    print(f"Parsed {len(all_postings)} postings ({sum(1 for p in all_postings if p['chancenbonus'])} at Chancenbonus schools)")

    print("Geocoding schools...")
    geo_cache = geocode_schools(all_postings)

    previous = load_previous()
    path = save_snapshot(all_postings)
    print(f"Snapshot saved to {path}")

    if previous is not None:
        added, removed = diff_postings(previous, all_postings)
        new_keys = {posting_key(p) for p in added}
    else:
        added, removed = [], []
        new_keys = set()

    generate_html(all_postings, geo_cache, new_keys)

    if previous is None:
        print("First run — no previous data to compare.")
        return

    if not added and not removed:
        print("No changes since last snapshot.")
        return

    # Apply filters to added/removed for notification
    filters = config.get("filters", {})
    filtered_added = apply_filters(added, filters)
    filtered_removed = apply_filters(removed, filters)

    print(f"\nChanges (total): +{len(added)} / -{len(removed)}")
    if filters.get("schultyp") or filters.get("bildungsregion") or filters.get("bezirk") or filters.get("chancenbonus_only"):
        print(f"Changes (filtered): +{len(filtered_added)} / -{len(filtered_removed)}")

    if filtered_added:
        print(f"\n--- {len(filtered_added)} NEUE Stellen ---")
        for p in filtered_added:
            print(format_posting(p))

    if filtered_removed:
        print(f"\n--- {len(filtered_removed)} ENTFERNTE Stellen ---")
        for p in filtered_removed:
            print(format_posting(p))

    if filtered_added or filtered_removed:
        send_email(config, filtered_added, filtered_removed)


if __name__ == "__main__":
    main()
