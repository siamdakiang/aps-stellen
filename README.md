# APS Stellen Tracker

Tracks open teaching positions (Stellenausschreibungen) for Landeslehrer at Allgemeinbildende Pflichtschulen (APS) in Oberösterreich. Fetches data daily from the Bildungsdirektion OÖ, stores historical snapshots, detects changes, and optionally sends email notifications.

Each posting is enriched with the **Chancenbonus** flag — schools participating in the federal Chancenbonus-Programm (Stand: 9.3.2026).

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python fetch.py
```

First run creates a snapshot in `data/`. Subsequent runs compare against the previous snapshot and report new/removed postings.

## Configuration

Edit `config.yaml`:

- **filters**: Restrict notifications to specific Schultyp, Bildungsregion, Bezirk, or Chancenbonus schools only
- **email**: Configure SMTP settings for email notifications (set `enabled: true`)

For GitHub Actions, store `SMTP_PASSWORD` as a repository secret.

## GitHub Actions

The workflow runs daily at 07:00 CET and commits new snapshots to the repo. Trigger manually via:

```bash
gh workflow run fetch.yml
```

## Data Source

- Postings: https://info.bildung-ooe.gv.at/data/Bewerbung_APS.xml
- Chancenbonus schools: [BMB list (PDF)](https://www.bmb.gv.at/Themen/schule/zrp/chancenbonus.html)
