#!/usr/bin/env python3
"""Fetch APS teacher job postings from Bildungsdirektion OÖ and track changes."""

import json
import os
import re
import smtplib
import subprocess
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
PROFILES_CACHE = SCRIPT_DIR / "school_profiles.json"

FACILITY_KEYWORDS = {
    "smartboard": "Smartboard",
    "activepanel": "Smartboard",
    "beamer": "Beamer",
    "schulgarten": "Schulgarten",
    "ganztagsschule": "Ganztags",
    "ganztägig": "Ganztags",
    "nachmittagsbetreuung": "Nachmittagsbetreuung",
    "bibliothek": "Bibliothek",
    "turnsaal": "Turnsaal",
    "turnhalle": "Turnsaal",
    "ipad": "iPads/Tablets",
    "tablet": "iPads/Tablets",
    "wlan": "WLAN",
    "digitale grundbildung": "Digitale Schule",
    "werkraum": "Werkraum",
    "musikraum": "Musikraum",
}

STATISTIK_WFS_URL = (
    "https://www.statistik.at/gs-atlas/ATLAS_SCHULE_WFS/ows"
    "?service=WFS&version=1.0.0&request=GetFeature"
    "&typeName=ATLAS_SCHULE_WFS:ATLAS_SCHULE"
    "&outputFormat=application%2Fjson"
    "&CQL_FILTER=GMNR%20LIKE%20%274%25%27"
    "&srsName=EPSG:4326&maxFeatures=2000"
)

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


def fetch_school_stats():
    """Fetch school statistics for all OÖ schools from Statistik Austria WFS."""
    try:
        resp = requests.get(STATISTIK_WFS_URL, timeout=30,
                            headers={"User-Agent": "APS-Stellen-Tracker/1.0"})
        resp.raise_for_status()
        data = resp.json()
        stats = {}
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            skz = props.get("SKZ")
            if not skz:
                continue
            stats[skz] = {
                "name": props.get("BEZEICHNUNG", ""),
                "address": f'{props.get("STR", "")}, {props.get("PLZ", "")} {props.get("ORT", "")}',
                "students": props.get("SCHUELER_INSG"),
                "classes": props.get("KLASSEN"),
                "school_type": props.get("KARTO_TYP", ""),
            }
        print(f"  Fetched stats for {len(stats)} OÖ schools from Statistik Austria")
        return stats
    except Exception as e:
        print(f"  Warning: Could not fetch school stats: {e}")
        return {}


def scrape_facility_keywords(url):
    """Fetch a school website and scan for facility keywords."""
    try:
        resp = requests.get(url, timeout=10,
                            headers={"User-Agent": "APS-Stellen-Tracker/1.0"})
        resp.raise_for_status()
        text = resp.text.lower()
        found = set()
        for keyword, label in FACILITY_KEYWORDS.items():
            if keyword in text:
                found.add(label)
        return sorted(found)
    except Exception:
        return []


def find_school_website(skz, school_name):
    """Try to find a school's website URL using common OÖ school URL patterns."""
    # Try eduhi.at pattern (common for OÖ schools)
    # Also try searching Nominatim/OSM for website tag
    # For now, construct eduhi.at search URL and try it
    patterns = []

    # Extract school type prefix and number (e.g., "VS 16" from "VS 16 Linz, Sonnensteinschule")
    m = re.match(r'(VS|MS|ASO|PTS)\s*(\d+)?\s*(.*)', school_name)
    if m:
        stype = m.group(1).lower()
        num = m.group(2) or ""
        rest = m.group(3).strip().rstrip(",").strip()
        city = rest.split(",")[0].strip().lower() if "," in rest else rest.lower()
        # Common URL patterns
        if num:
            patterns.append(f"https://{stype}{num}{city}.eduhi.at")
            patterns.append(f"https://www.{stype}{num}{city}.at")
        patterns.append(f"https://{stype}-{city}.eduhi.at")

    for url in patterns:
        try:
            resp = requests.head(url, timeout=5, allow_redirects=True,
                                 headers={"User-Agent": "APS-Stellen-Tracker/1.0"})
            if resp.status_code < 400:
                return resp.url
        except Exception:
            continue
    return None


def enrich_school_profiles(postings, geo_cache):
    """Enrich school profiles with stats and facility data."""
    cache = {}
    if PROFILES_CACHE.exists():
        with open(PROFILES_CACHE) as f:
            cache = json.load(f)

    # Collect unique schools from postings
    unique_schools = {}
    for p in postings:
        skz = p["schulkennzahl"]
        if skz and skz not in unique_schools:
            unique_schools[skz] = p

    now_str = datetime.now().strftime("%Y-%m-%d")
    new_count = 0

    # Fetch all OÖ school stats in one batch request
    needs_stats = any(
        skz not in cache or not cache.get(skz, {}).get("stats")
        for skz in unique_schools
    )
    all_stats = {}
    if needs_stats:
        all_stats = fetch_school_stats()

    for skz, p in unique_schools.items():
        if skz not in cache:
            cache[skz] = {}

        profile = cache[skz]

        # Stats: update if missing
        if not profile.get("stats") and skz in all_stats:
            st = all_stats[skz]
            profile["stats"] = {
                "students": st["students"],
                "classes": st["classes"],
                "address": st["address"],
                "fetched_at": now_str,
            }
            new_count += 1

        # Facilities: scrape website if not yet done
        if not profile.get("facilities"):
            # Try to find school website
            website_url = profile.get("website_url")
            if not website_url:
                website_url = find_school_website(skz, p["school_name"])
                if website_url:
                    profile["website_url"] = website_url
                    time.sleep(0.5)

            if website_url:
                keywords = scrape_facility_keywords(website_url)
                profile["facilities"] = {
                    "keywords": keywords,
                    "fetched_at": now_str,
                }
                new_count += 1
                time.sleep(0.5)

    if new_count > 0:
        with open(PROFILES_CACHE, "w") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print(f"  Enriched {new_count} school profiles")

    return cache


def import_community_reviews(profiles):
    """Import approved school reviews from GitHub Issues via gh CLI."""
    try:
        result = subprocess.run(
            ["gh", "issue", "list",
             "--label", "school-review",
             "--label", "approved",
             "--state", "all",
             "--json", "number,title,body",
             "--limit", "500"],
            capture_output=True, text=True, timeout=30,
            cwd=SCRIPT_DIR,
        )
        if result.returncode != 0:
            return profiles

        issues = json.loads(result.stdout) if result.stdout.strip() else []
        if not issues:
            return profiles

        # Parse each issue and aggregate reviews by SKZ
        reviews_by_skz = {}
        for issue in issues:
            body = issue.get("body", "")
            # Parse structured GitHub Issue form responses
            skz = _extract_field(body, "Schulkennzahl")
            if not skz or len(skz) != 6:
                continue

            review = {
                "fuehrung": _extract_rating(body, "Führung"),
                "team": _extract_rating(body, "Team"),
                "ausstattung": _extract_rating(body, "Ausstattung"),
                "atmosphaere": _extract_rating(body, "Atmosphäre"),
                "fuehrung_text": _extract_field(body, "Kommentar zur Führung"),
                "team_text": _extract_field(body, "Kommentar zum Team"),
                "ausstattung_text": _extract_field(body, "Kommentar zur Ausstattung"),
                "atmosphaere_text": _extract_field(body, "Kommentar zur Atmosphäre"),
                "extra": _extract_field(body, "Sonstiges"),
            }

            if skz not in reviews_by_skz:
                reviews_by_skz[skz] = []
            reviews_by_skz[skz].append(review)

        # Aggregate into profiles
        changed = False
        for skz, reviews in reviews_by_skz.items():
            if skz not in profiles:
                profiles[skz] = {}

            community = {"review_count": len(reviews)}
            for dim in ["fuehrung", "team", "ausstattung", "atmosphaere"]:
                scores = [r[dim] for r in reviews if r[dim]]
                comments = [r[f"{dim}_text"] for r in reviews if r.get(f"{dim}_text")]
                community[dim] = {
                    "avg": round(sum(scores) / len(scores), 1) if scores else None,
                    "comments": comments,
                }

            # Overall average
            all_scores = []
            for dim in ["fuehrung", "team", "ausstattung", "atmosphaere"]:
                if community[dim]["avg"]:
                    all_scores.append(community[dim]["avg"])
            community["overall_avg"] = round(sum(all_scores) / len(all_scores), 1) if all_scores else None
            community["updated_at"] = datetime.now().strftime("%Y-%m-%d")

            profiles[skz]["community"] = community
            changed = True

        if changed:
            with open(PROFILES_CACHE, "w") as f:
                json.dump(profiles, f, ensure_ascii=False, indent=2)
            print(f"  Imported community reviews for {len(reviews_by_skz)} schools")

    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        # gh CLI not available or timeout — skip silently
        pass
    except Exception as e:
        print(f"  Warning: Could not import community reviews: {e}")

    return profiles


def _extract_field(body, label):
    """Extract a field value from GitHub Issue form body."""
    # GitHub Issue forms use "### Label\n\nValue" format
    pattern = rf'### {re.escape(label)}\s*\n\n(.+?)(?:\n\n###|\Z)'
    m = re.search(pattern, body, re.DOTALL)
    if m:
        val = m.group(1).strip()
        if val and val != "_No response_":
            return val
    return ""


def _extract_rating(body, label):
    """Extract a numeric rating from GitHub Issue form dropdown."""
    val = _extract_field(body, label)
    if val:
        m = re.match(r'(\d)', val)
        if m:
            return int(m.group(1))
    return None


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


def normalize_for_key(text):
    """Normalize text for stable key generation: collapse whitespace, normalize punctuation."""
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\s*,\s*', ',', text)
    text = re.sub(r'\s*\(\s*', '(', text)
    text = re.sub(r'\s*\)\s*', ')', text)
    return text


def posting_key(p):
    return f"{p['dienststelle']}|{normalize_for_key(p['schulfach'])}"


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

    # Transition guard: if >80% appear "new", it's likely a key format change
    # (e.g. after deploying normalization). Fall back to dienststelle-only matching.
    if previous and len(added) > 0.8 * len(current):
        prev_dien = {p['dienststelle']: p for p in previous}
        curr_dien = {p['dienststelle']: p for p in current}
        added = [curr_dien[d] for d in curr_dien if d not in prev_dien]
        removed = [prev_dien[d] for d in prev_dien if d not in curr_dien]

    return added, removed


def format_posting(p):
    cb = " [CHANCENBONUS]" if p["chancenbonus"] else ""
    return (
        f"  {p['school_name']}{cb}\n"
        f"    Schultyp: {p['schultyp']} | Bezirk: {p['bezirk']} | Region: {p['bildungsregion']}\n"
        f"    Fach: {p['schulfach']}\n"
        f"    Frist: {p['befristet_date'] or 'k.A.'} | Bewerber: {p['bewerber']}"
    )


def format_html_email(added, removed):
    """Generate a styled HTML email body for posting changes."""
    date_str = datetime.now().strftime("%d.%m.%Y")

    def iso_to_at(iso_date):
        if not iso_date:
            return "k.A."
        try:
            parts = iso_date.split("-")
            return f"{parts[2]}.{parts[1]}.{parts[0]}"
        except (IndexError, ValueError):
            return iso_date

    def posting_rows(postings):
        rows = []
        for p in postings:
            cb_tag = (' <span style="background:#d1fae5;color:#059669;padding:2px 8px;'
                       'border-radius:12px;font-size:11px;font-weight:600;">Chancenbonus</span>'
                       if p.get("chancenbonus") else "")
            bg = "#f0fdf4" if p.get("chancenbonus") else "#ffffff"
            frist = iso_to_at(p.get("befristet_date"))
            rows.append(
                f'<tr style="background:{bg};">'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-weight:500;">'
                f'{p.get("school_name", "")}{cb_tag}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{p.get("schultyp", "")}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{p.get("bezirk", "")}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{p.get("fach_label", "")}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:center;">'
                f'{p.get("hours", 0)}h</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{frist}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:center;'
                f'{"color:#059669;font-weight:600;" if p.get("bewerber", 0) == 0 else ""}">'
                f'{p.get("bewerber", 0)}</td>'
                f'</tr>'
            )
        return "\n".join(rows)

    table_head = (
        '<tr style="background:#374151;color:#fff;">'
        '<th style="padding:8px 12px;text-align:left;font-size:13px;">Schule</th>'
        '<th style="padding:8px 12px;text-align:left;font-size:13px;">Schultyp</th>'
        '<th style="padding:8px 12px;text-align:left;font-size:13px;">Bezirk</th>'
        '<th style="padding:8px 12px;text-align:left;font-size:13px;">Fach</th>'
        '<th style="padding:8px 12px;text-align:center;font-size:13px;">Stunden</th>'
        '<th style="padding:8px 12px;text-align:left;font-size:13px;">Frist</th>'
        '<th style="padding:8px 12px;text-align:center;font-size:13px;">Bewerber</th>'
        '</tr>'
    )

    parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>',
        '<body style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;'
        'margin:0;padding:0;background:#f3f4f6;">',
        '<div style="background:linear-gradient(135deg,#1a56db,#1e40af);color:#fff;padding:24px 32px;">',
        '<h1 style="margin:0;font-size:20px;">APS Stellen Update</h1>',
        f'<p style="margin:4px 0 0;opacity:0.85;font-size:14px;">{date_str} &mdash; '
        f'{len(added)} neue, {len(removed)} entfernte Stellen</p>',
        '</div>',
        '<div style="padding:24px 32px;">',
    ]

    if added:
        parts.append(f'<h2 style="color:#059669;font-size:16px;margin:16px 0 8px;">'
                      f'&#x2795; {len(added)} Neue Stellen</h2>')
        parts.append('<table style="border-collapse:collapse;width:100%;background:#fff;'
                      'border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);">')
        parts.append(table_head)
        parts.append(posting_rows(added))
        parts.append('</table>')

    if removed:
        parts.append(f'<h2 style="color:#e11d48;font-size:16px;margin:24px 0 8px;">'
                      f'&#x274c; {len(removed)} Entfernte Stellen</h2>')
        parts.append('<table style="border-collapse:collapse;width:100%;background:#fff;'
                      'border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);">')
        parts.append(table_head)
        parts.append(posting_rows(removed))
        parts.append('</table>')

    parts.extend([
        '<div style="margin-top:24px;padding-top:16px;border-top:1px solid #e5e7eb;'
        'font-size:13px;color:#6b7280;">',
        '<p><a href="https://siamdakiang.github.io/aps-stellen/" style="color:#1a56db;">'
        'Dashboard ansehen</a>',
        ' &middot; <a href="https://bewerbung.bildung.gv.at/app/portal/#/app/bewo" '
        'style="color:#1a56db;">Zum Bewerbungsportal</a></p>',
        '</div>',
        '</div></body></html>',
    ])

    return "\n".join(parts)


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

    body = format_html_email(added, removed)

    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = f"APS Stellen: {len(added)} neu, {len(removed)} entfernt — {datetime.now().strftime('%d.%m.%Y')}"
    msg["From"] = email_from
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)

    print(f"Email sent to {', '.join(recipients)}")


def generate_html(postings, geo_cache=None, new_keys=None, profiles=None):
    docs_dir = SCRIPT_DIR / "docs"
    docs_dir.mkdir(exist_ok=True)

    geo_cache = geo_cache or {}
    new_keys = new_keys or set()
    profiles = profiles or {}
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

    # Build profiles JSON for embedding in HTML
    profiles_json = json.dumps(profiles, ensure_ascii=False)

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
        # Profile badge
        prof = profiles.get(skz, {})
        community = prof.get("community", {})
        overall_avg = community.get("overall_avg")
        has_profile = bool(prof.get("stats") or prof.get("facilities") or prof.get("community"))
        if overall_avg:
            rating_color = "rating-good" if overall_avg >= 4.0 else ("rating-ok" if overall_avg >= 3.0 else "rating-low")
            profile_badge = f' <span class="badge profile-badge {rating_color}" onclick="event.stopPropagation();showProfile(\'{html_esc(skz)}\')">{overall_avg} ★ ({community.get("review_count", 0)})</span>'
        elif has_profile:
            profile_badge = f' <span class="badge profile-badge profile-info" onclick="event.stopPropagation();showProfile(\'{html_esc(skz)}\')">ℹ</span>'
        else:
            profile_badge = ""

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
            f'<td><span class="school-name school-link" onclick="showProfile(\'{html_esc(skz)}\')">{html_esc(p["school_name"])}</span>{cb_badge}{new_badge}{profile_badge}</td>'
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
.chip.active[data-group="cb"][data-value="1"] {{ border-color: var(--green); background: var(--green-light); color: var(--green); }}
.chip.active[data-group="cb"][data-value="0"] {{ border-color: var(--rose); background: var(--rose-light); color: var(--rose); }}
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
.school-link {{ cursor: pointer; color: var(--primary); }}
.school-link:hover {{ text-decoration: underline; }}
.profile-badge {{ cursor: pointer; }}
.rating-good {{ background: var(--green-light); color: var(--green); }}
.rating-ok {{ background: var(--amber-light); color: var(--amber); }}
.rating-low {{ background: var(--rose-light); color: var(--rose); }}
.profile-info {{ background: var(--primary-light); color: var(--primary); font-size: 0.65rem; }}
/* Modal */
.modal-overlay {{
  display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.5); z-index: 100; justify-content: center; align-items: center;
  backdrop-filter: blur(2px);
}}
.modal-overlay.active {{ display: flex; }}
.modal {{
  background: #fff; border-radius: 12px; max-width: 560px; width: 90%;
  max-height: 85vh; overflow-y: auto; position: relative;
  box-shadow: 0 20px 60px rgba(0,0,0,0.3);
}}
.modal-header {{
  background: linear-gradient(135deg, var(--primary) 0%, #1e40af 100%);
  color: #fff; padding: 1.25rem 1.5rem; border-radius: 12px 12px 0 0;
}}
.modal-header h2 {{ font-size: 1.1rem; font-weight: 700; margin-bottom: 0.3rem; }}
.modal-header .modal-meta {{ display: flex; gap: 0.5rem; flex-wrap: wrap; }}
.modal-header .modal-meta .badge {{ background: rgba(255,255,255,0.2); color: #fff; }}
.modal-close {{
  position: absolute; top: 0.75rem; right: 0.75rem; background: rgba(255,255,255,0.2);
  border: none; color: #fff; font-size: 1.2rem; width: 2rem; height: 2rem; border-radius: 50%;
  cursor: pointer; display: flex; align-items: center; justify-content: center;
}}
.modal-close:hover {{ background: rgba(255,255,255,0.3); }}
.modal-body {{ padding: 1.25rem 1.5rem; }}
.modal-section {{ margin-bottom: 1.25rem; }}
.modal-section:last-child {{ margin-bottom: 0; }}
.modal-section-title {{
  font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--gray-500); margin-bottom: 0.5rem;
}}
.modal-stats {{ display: flex; gap: 1rem; }}
.modal-stat {{
  flex: 1; text-align: center; padding: 0.75rem; background: var(--gray-50);
  border-radius: 8px; border: 1px solid var(--gray-200);
}}
.modal-stat .num {{ font-size: 1.3rem; font-weight: 700; color: var(--gray-900); }}
.modal-stat .label {{ font-size: 0.7rem; color: var(--gray-500); margin-top: 0.1rem; }}
.dim-row {{ display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.4rem; }}
.dim-label {{ font-size: 0.8rem; font-weight: 600; width: 90px; color: var(--gray-700); }}
.dim-bar-wrap {{ flex: 1; height: 8px; background: var(--gray-200); border-radius: 4px; overflow: hidden; }}
.dim-bar {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
.dim-score {{ font-size: 0.8rem; font-weight: 600; width: 24px; text-align: right; }}
.dim-comment {{ font-size: 0.75rem; color: var(--gray-500); margin: 0 0 0.3rem 90px; padding-left: 0.75rem; font-style: italic; }}
.facility-chips {{ display: flex; flex-wrap: wrap; gap: 0.4rem; }}
.facility-chip {{
  padding: 0.25rem 0.6rem; border-radius: 6px; font-size: 0.75rem; font-weight: 500;
  background: var(--primary-light); color: var(--primary); border: 1px solid transparent;
}}
.modal-links {{ display: flex; flex-wrap: wrap; gap: 0.5rem; }}
.modal-link {{
  padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.8rem; font-weight: 500;
  text-decoration: none; border: 1px solid var(--gray-300); color: var(--gray-700);
}}
.modal-link:hover {{ background: var(--gray-100); }}
.modal-link.primary {{ background: var(--primary); color: #fff; border-color: var(--primary); }}
.modal-link.primary:hover {{ background: #1e40af; }}
.modal-empty {{ font-size: 0.85rem; color: var(--gray-500); font-style: italic; }}
@media (max-width: 768px) {{
  .header, .controls, .table-wrap, .count {{ padding-left: 1rem; padding-right: 1rem; }}
  .filter-label {{ min-width: auto; width: 100%; }}
  .header .stats {{ flex-wrap: wrap; gap: 0.75rem; }}
  .modal {{ width: 95%; max-height: 90vh; }}
  .dim-label {{ width: 70px; font-size: 0.75rem; }}
  .dim-comment {{ margin-left: 70px; }}
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
    <button class="chip" data-group="cb" data-value="0" onclick="toggleChip(this)">Ohne Chancenbonus</button>
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
const PROFILES = {profiles_json};
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
    if (vis && filters.cb.size && !filters.cb.has(r.dataset.cb)) vis = false;
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

    const RUSH_MULTIPLIER = 1.2; // 6:30-7:30 early rush hour
    const durations = osrmData.durations[0]; // seconds from user to each school

    // Update table cells
    rows.forEach(r => {{
      const skz = r.dataset.skz;
      const cell = r.cells[8];
      if (skz && skz in skzToIdx) {{
        const secs = durations[skzToIdx[skz]];
        if (secs !== null) {{
          const mins = Math.round((secs * RUSH_MULTIPLIER) / 60);
          cell.textContent = "~" + mins + " min";
          cell.dataset.minutes = mins;
          if (mins <= 25) cell.className = "commute-cell commute-short";
          else if (mins <= 50) cell.className = "commute-cell commute-medium";
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

    status.textContent = "Typische Pendelzeit (Auto, 6:30\u20137:30 Uhr)";

    // Auto-sort by commute time
    sortDir[8] = false;
    sortTable(8);

  }} catch(e) {{
    status.textContent = "Fehler: " + e.message;
  }}
  btn.disabled = false;
}}
function showProfile(skz) {{
  const p = PROFILES[skz];
  if (!p) return;

  const overlay = document.getElementById("profileOverlay");
  const content = document.getElementById("profileContent");

  // Find school info from the table row
  const row = document.querySelector(`tr[data-skz="${{skz}}"]`);
  const schoolName = row ? row.querySelector(".school-name").textContent : skz;
  const schultyp = row ? row.dataset.schultyp : "";
  const bezirk = row ? row.dataset.bezirk : "";
  const isCB = row ? row.dataset.cb === "1" : false;

  let html = "";

  // Header
  html += `<div class="modal-header">`;
  html += `<h2>${{schoolName}}</h2>`;
  html += `<div class="modal-meta">`;
  if (schultyp) html += `<span class="badge">${{schultyp}}</span>`;
  if (bezirk) html += `<span class="badge">${{bezirk}}</span>`;
  if (isCB) html += `<span class="badge" style="background:rgba(16,185,129,0.3)">Chancenbonus</span>`;
  html += `</div></div>`;

  html += `<div class="modal-body">`;

  // Community reviews
  const c = p.community;
  if (c && c.overall_avg) {{
    html += `<div class="modal-section">`;
    html += `<div class="modal-section-title">Lehrerbewertungen</div>`;
    const stars = "★".repeat(Math.round(c.overall_avg)) + "☆".repeat(5 - Math.round(c.overall_avg));
    html += `<div style="font-size:1.1rem;margin-bottom:0.6rem">${{stars}} <strong>${{c.overall_avg}}</strong> <span style="color:var(--gray-500);font-size:0.8rem">(${{c.review_count}} Bewertung${{c.review_count !== 1 ? "en" : ""}})</span></div>`;

    const dims = [
      ["fuehrung", "Führung"],
      ["team", "Team"],
      ["ausstattung", "Ausstattung"],
      ["atmosphaere", "Atmosphäre"],
    ];
    dims.forEach(([key, label]) => {{
      const d = c[key];
      if (!d || !d.avg) return;
      const pct = (d.avg / 5) * 100;
      const color = d.avg >= 4 ? "var(--green)" : d.avg >= 3 ? "var(--amber)" : "var(--rose)";
      html += `<div class="dim-row">`;
      html += `<span class="dim-label">${{label}}</span>`;
      html += `<div class="dim-bar-wrap"><div class="dim-bar" style="width:${{pct}}%;background:${{color}}"></div></div>`;
      html += `<span class="dim-score" style="color:${{color}}">${{d.avg}}</span>`;
      html += `</div>`;
      if (d.comments && d.comments.length) {{
        d.comments.forEach(txt => {{
          if (txt) html += `<div class="dim-comment">"${{txt.substring(0, 120)}}${{txt.length > 120 ? "…" : ""}}"</div>`;
        }});
      }}
    }});
    html += `</div>`;
  }}

  // School stats
  const st = p.stats;
  if (st) {{
    html += `<div class="modal-section">`;
    html += `<div class="modal-section-title">Schulgröße (2024/25)</div>`;
    html += `<div class="modal-stats">`;
    if (st.students) html += `<div class="modal-stat"><div class="num">${{st.students}}</div><div class="label">Schüler·innen</div></div>`;
    if (st.classes) html += `<div class="modal-stat"><div class="num">${{st.classes}}</div><div class="label">Klassen</div></div>`;
    html += `</div>`;
    if (st.address) html += `<div style="font-size:0.75rem;color:var(--gray-500);margin-top:0.4rem">${{st.address}}</div>`;
    html += `</div>`;
  }}

  // Facilities
  const f = p.facilities;
  if (f && f.keywords && f.keywords.length) {{
    html += `<div class="modal-section">`;
    html += `<div class="modal-section-title">Ausstattung</div>`;
    html += `<div class="facility-chips">`;
    f.keywords.forEach(kw => {{
      html += `<span class="facility-chip">${{kw}}</span>`;
    }});
    html += `</div></div>`;
  }}

  // No data at all?
  if (!c?.overall_avg && !st && (!f || !f.keywords?.length)) {{
    html += `<div class="modal-section"><p class="modal-empty">Noch keine Profildaten vorhanden. Hilf mit und bewerte diese Schule!</p></div>`;
  }}

  // Links
  html += `<div class="modal-section">`;
  html += `<div class="modal-section-title">Links</div>`;
  html += `<div class="modal-links">`;
  const mapsQ = encodeURIComponent(schoolName + ", Oberösterreich, Austria");
  html += `<a class="modal-link" href="https://www.google.com/maps/search/${{mapsQ}}" target="_blank" rel="noopener">🗺 Google Maps</a>`;
  if (p.website_url) html += `<a class="modal-link" href="${{p.website_url}}" target="_blank" rel="noopener">🌐 Website</a>`;
  html += `<a class="modal-link" href="https://bewerbung.bildung.gv.at/app/portal/#/app/bewo" target="_blank" rel="noopener">📝 Bewerben</a>`;
  const reviewUrl = "https://github.com/siamdakiang/aps-stellen/issues/new?template=school_review.yml&title=" + encodeURIComponent("Schulbewertung: " + schoolName + " (" + skz + ")");
  html += `<a class="modal-link primary" href="${{reviewUrl}}" target="_blank" rel="noopener">⭐ Schule bewerten</a>`;
  html += `</div></div>`;

  html += `</div>`; // modal-body
  content.innerHTML = html;
  overlay.classList.add("active");
  document.body.style.overflow = "hidden";
}}

function closeProfile(e) {{
  if (e && e.target !== e.currentTarget) return;
  document.getElementById("profileOverlay").classList.remove("active");
  document.body.style.overflow = "";
}}
document.addEventListener("keydown", e => {{ if (e.key === "Escape") closeProfile(); }});
</script>
<div id="profileOverlay" class="modal-overlay" onclick="closeProfile(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <button class="modal-close" onclick="closeProfile()">&times;</button>
    <div id="profileContent"></div>
  </div>
</div>
<div class="footer">
  <div>Daten: <a href="https://info.bildung-ooe.gv.at/stellenAPS.html" target="_blank" rel="noopener">Bildungsdirektion O\u00d6</a> &mdash; t\u00e4glich automatisch aktualisiert</div>
  <div style="margin-top:0.3rem">
    Von <strong>Simon Ludwig</strong>
    <span>&middot;</span>
    <a href="https://github.com/siamdakiang/aps-stellen" target="_blank" rel="noopener">GitHub</a>
    <span>&middot;</span>
    Built with <a href="https://claude.ai/claude-code" target="_blank" rel="noopener">Claude Code</a>
    <span>&middot;</span>
    ~500k tokens spent
  </div>
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

    print("Enriching school profiles...")
    profiles = enrich_school_profiles(all_postings, geo_cache)
    profiles = import_community_reviews(profiles)

    previous = load_previous()
    path = save_snapshot(all_postings)
    print(f"Snapshot saved to {path}")

    if previous is not None:
        added, removed = diff_postings(previous, all_postings)
        new_keys = {posting_key(p) for p in added}
    else:
        added, removed = [], []
        new_keys = set()

    generate_html(all_postings, geo_cache, new_keys, profiles)

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
