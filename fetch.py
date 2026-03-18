#!/usr/bin/env python3
"""Fetch APS teacher job postings from Bildungsdirektion OÖ and track changes."""

import json
import os
import smtplib
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"

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


def fetch_xml(url):
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def excel_serial_to_date(serial):
    try:
        return (EXCEL_EPOCH + timedelta(days=int(serial))).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


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

        postings.append({
            "bezeichnung": (stelle.findtext("BEZEICHNUNG") or "").strip(),
            "dienststelle": dienststelle,
            "schulkennzahl": code,
            "school_name": school_name,
            "schulfach": (stelle.findtext("SCHULFACH") or "").strip(),
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


def generate_html(postings):
    docs_dir = SCRIPT_DIR / "docs"
    docs_dir.mkdir(exist_ok=True)

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    cb_count = sum(1 for p in postings if p["chancenbonus"])

    rows = []
    for p in sorted(postings, key=lambda x: (x["bildungsregion"], x["bezirk"], x["school_name"])):
        cb = ' <span class="cb">CB</span>' if p["chancenbonus"] else ""
        rows.append(
            f"<tr>"
            f'<td>{html_esc(p["school_name"])}{cb}</td>'
            f'<td>{html_esc(p["schultyp"])}</td>'
            f'<td>{html_esc(p["bezirk"])}</td>'
            f'<td>{html_esc(p["bildungsregion"])}</td>'
            f'<td>{html_esc(p["schulfach"])}</td>'
            f'<td>{html_esc(p["befristet_date"] or "k.A.")}</td>'
            f'<td>{p["bewerber"]}</td>'
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>APS Stellen OÖ</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 2rem; background: #f8f9fa; color: #212529; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.3rem; }}
  .meta {{ color: #6c757d; font-size: 0.9rem; margin-bottom: 1.5rem; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  th, td {{ padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid #dee2e6; font-size: 0.85rem; }}
  th {{ background: #343a40; color: #fff; position: sticky; top: 0; cursor: pointer; }}
  th:hover {{ background: #495057; }}
  tr:hover {{ background: #f1f3f5; }}
  .cb {{ background: #28a745; color: #fff; padding: 1px 5px; border-radius: 3px; font-size: 0.75rem; font-weight: 600; }}
  input {{ padding: 0.4rem 0.75rem; font-size: 0.9rem; border: 1px solid #ced4da; border-radius: 4px; width: 300px; margin-bottom: 1rem; }}
</style>
</head>
<body>
<h1>Offene APS-Stellen Oberösterreich</h1>
<p class="meta">{len(postings)} Stellen ({cb_count} an Chancenbonus-Schulen) &mdash; Stand: {now}</p>
<input type="text" id="filter" placeholder="Filtern (Schule, Bezirk, Fach...)" onkeyup="filterTable()">
<table id="stellen">
<thead><tr>
<th onclick="sortTable(0)">Schule</th>
<th onclick="sortTable(1)">Schultyp</th>
<th onclick="sortTable(2)">Bezirk</th>
<th onclick="sortTable(3)">Region</th>
<th onclick="sortTable(4)">Fach / Details</th>
<th onclick="sortTable(5)">Frist</th>
<th onclick="sortTable(6)">Bewerber</th>
</tr></thead>
<tbody>
{"".join(rows)}
</tbody>
</table>
<script>
function filterTable() {{
  const q = document.getElementById("filter").value.toLowerCase();
  document.querySelectorAll("#stellen tbody tr").forEach(r => {{
    r.style.display = r.textContent.toLowerCase().includes(q) ? "" : "none";
  }});
}}
let sortDir = {{}};
function sortTable(col) {{
  const tb = document.querySelector("#stellen tbody");
  const rows = Array.from(tb.rows);
  sortDir[col] = !sortDir[col];
  rows.sort((a, b) => {{
    let x = a.cells[col].textContent, y = b.cells[col].textContent;
    if (col === 6) return sortDir[col] ? x - y : y - x;
    return sortDir[col] ? x.localeCompare(y, "de") : y.localeCompare(x, "de");
  }});
  rows.forEach(r => tb.appendChild(r));
}}
</script>
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

    previous = load_previous()
    path = save_snapshot(all_postings)
    print(f"Snapshot saved to {path}")

    generate_html(all_postings)

    if previous is None:
        print("First run — no previous data to compare.")
        return

    added, removed = diff_postings(previous, all_postings)

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
