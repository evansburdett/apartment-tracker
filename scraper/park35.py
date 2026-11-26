"""Daily Park 35 on Clairmont scraper.

Pulls every available unit from Park 35's own Entrata site, the view/porch
tags for each unit from its floor plan page, and the rent calculator matrix
(price by move-in date and lease term) for 2BR units that are not ruled out.

Fetch order per request: direct (curl_cffi, Chrome fingerprint) first, then
ScrapingBee premium proxy if Cloudflare blocks the direct request and
SCRAPINGBEE_API_KEY is set. Requests are sequential with a delay, because the
site rate-limits bursts (HTTP 429).

Outputs (relative to repo root):
  data/latest.json          full snapshot for today
  data/units_history.csv    one row per unit per day
  data/movein_history.csv   one row per unit x move-in date x lease term per day
  raw/                      raw HTML from the latest run, for debugging parsers
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = ROOT / "raw"
BASE = "https://www.park35onclairmont.com"
PROPERTY_ID = "1128707"
BEANS_MAP = (f"{BASE}/Apartments/module/property_info/action/view_beans_map/"
             f"property%5Bid%5D/{PROPERTY_ID}/?occupancy_type=conventional&is_filter_request=0")
FLOORPLAN_INDEX_PAGES = [f"{BASE}/", f"{BASE}/birmingham/park-35-on-clairmont-mt/conventional/"]
DELAY_S = float(os.environ.get("REQUEST_DELAY_S", "4"))
MOVE_IN_OFFSETS_DAYS = [int(x) for x in os.environ.get("MOVE_IN_OFFSETS", "7,14,21,30,45,60,90").split(",")]
TZ = dt.timezone(dt.timedelta(hours=-6))  # America/Chicago standard time; date only

BLOCK_MARKERS = ("Performing security verification", "Just a moment", "cf-chl", "Verify you are human")
MAINTENANCE_MARKER = "Scheduled Maintenance"

VIEW_RULES = [  # (regex on tag, normalized view)
    (re.compile(r"golf course view", re.I), "Golf course"),
    (re.compile(r"pool view", re.I), "Pool"),
    (re.compile(r"wall view|garage", re.I), "Garage/Wall"),
    (re.compile(r"courtyard", re.I), "Courtyard"),
    (re.compile(r"street view|city view|park view|tree view|exterior", re.I), "Street/exterior"),
]
TAG_VOCAB = re.compile(
    r"(Golf Course View ?\d*|Pool view|Wall View ?\d*|Juliet Balcony|Balcony|Patio|Top Floor|"
    r"Courtyard View|Street View|City View|Corner|End Unit|Garage)", re.I)


class Fetcher:
    def __init__(self) -> None:
        self.session = creq.Session(impersonate="chrome")
        self.sb_key = os.environ.get("SCRAPINGBEE_API_KEY", "").strip()
        self.stats = {"direct_ok": 0, "direct_blocked": 0, "proxy_ok": 0, "proxy_fail": 0}
        self.last = 0.0

    def _wait(self) -> None:
        gap = time.time() - self.last
        if gap < DELAY_S:
            time.sleep(DELAY_S - gap)
        self.last = time.time()

    @staticmethod
    def _blocked(status: int, text: str) -> bool:
        return status in (403, 429, 503) or any(m in text[:5000] for m in BLOCK_MARKERS)

    def get(self, url: str, xhr: bool = False) -> str | None:
        headers = {"Accept-Language": "en-US,en;q=0.9"}
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"
        self._wait()
        try:
            r = self.session.get(url, headers=headers, timeout=45)
            if not self._blocked(r.status_code, r.text):
                self.stats["direct_ok"] += 1
                return r.text
            if MAINTENANCE_MARKER in r.text:
                raise SiteDown("Entrata scheduled maintenance")
            self.stats["direct_blocked"] += 1
        except SiteDown:
            raise
        except Exception as e:  # network error: fall through to proxy
            print(f"direct error {e!r} for {url}", file=sys.stderr)
            self.stats["direct_blocked"] += 1
        return self._proxy(url, headers)

    def _proxy(self, url: str, headers: dict) -> str | None:
        if not self.sb_key:
            return None
        params = {"api_key": self.sb_key, "url": url, "render_js": "false",
                  "premium_proxy": "true", "country_code": "us", "forward_headers": "true"}
        sb_headers = {f"Spb-{k}": v for k, v in headers.items()}
        for attempt in range(2):
            self._wait()
            try:
                r = creq.get("https://app.scrapingbee.com/api/v1/?" + urlencode(params),
                             headers=sb_headers, timeout=120)
                if r.status_code == 200 and not self._blocked(200, r.text):
                    self.stats["proxy_ok"] += 1
                    return r.text
                print(f"proxy status {r.status_code} for {url}: {r.text[:200]!r}", file=sys.stderr)
            except Exception as e:
                print(f"proxy error {e!r} for {url}", file=sys.stderr)
            params["render_js"] = "true"  # second attempt: let ScrapingBee solve JS challenges
        self.stats["proxy_fail"] += 1
        return None


class SiteDown(RuntimeError):
    pass


def money(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)", s)
    return float(m.group(1).replace(",", "")) if m else None


def save_raw(name: str, text: str | None) -> None:
    if text:
        RAW.mkdir(exist_ok=True)
        (RAW / name).write_text(text, encoding="utf-8")


# ---------- parsers ----------

UNIT_RE = re.compile(r"\b(\d)-([A-Z]?\d{2,3})\b")
TAG_UNIT_RE = re.compile(r"(?:\b\d-|#|\bApt\.?\s*|\bUnit\s+)([A-Z]?\d{2,3})\b")


def parse_beans_map(html: str) -> list[dict]:
    """Each available unit card has a Calculate button whose data-url carries
    unit_space[id] and property_floorplan[id]; the card text carries unit,
    plan, beds, baths, sqft, floor, availability and total monthly price."""
    # The map page embeds unit cards both as live HTML and as JSON-escaped strings.
    candidates = [html]
    if "\\/" in html:
        candidates.append(html.replace("\\/", "/").replace('\\"', '"').replace("\\n", "\n").replace("\\t", " "))
    units: dict[str, dict] = {}
    for doc in candidates:
        soup = BeautifulSoup(doc, "html.parser")
        for btn in soup.select("[data-url*='unit_space']"):
            url = btn.get("data-url", "").replace("&amp;", "&")
            us = re.search(r"unit_space\[id\]=(\d+)", url) or re.search(r"unit_space%5Bid%5D=(\d+)", url)
            fp = re.search(r"property_floorplan\[id\]=(\d+)", url) or re.search(r"property_floorplan%5Bid%5D=(\d+)", url)
            if not us:
                continue
            card, text = btn, ""
            for _ in range(10):
                card = card.parent
                if card is None:
                    break
                t = card.get_text("\n", strip=True)
                if len({m.group(2) for m in UNIT_RE.finditer(t)}) > 1 or len(t) >= 700:
                    break
                if UNIT_RE.search(t):
                    text = t
            if not text:
                continue
            m = UNIT_RE.search(text)
            unit = m.group(2)
            lines = [l for l in text.split("\n") if l.strip()]
            beds = re.search(r"(\d)\s*Bed", text)
            sqft = re.search(r"([\d,]{3,5})\s*SqFt", text, re.I)
            floor = re.search(r"Floor\s*(\d)", text)
            avail = re.search(r"(Available\s+(?:Now|[A-Z][a-z]{2} \d{2}, \d{4}))", text)
            total = re.search(r"Total Monthly Leasing Price\s*\$?([\d,]+\.\d{2})", text)
            idx = next((i for i, l in enumerate(lines) if UNIT_RE.search(l)), 0)
            plan = lines[idx + 1] if idx + 1 < len(lines) else None
            units[us.group(1)] = {
                "unit": unit,
                "unit_space_id": us.group(1),
                "floorplan_id": fp.group(1) if fp else None,
                "floorplan": plan,
                "beds": int(beds.group(1)) if beds else None,
                "sqft": int(sqft.group(1).replace(",", "")) if sqft else None,
                "floor": int(floor.group(1)) if floor else (0 if unit.startswith("G") else int(unit[0])),
                "available": avail.group(1).replace("Available ", "") if avail else None,
                "total_monthly_13mo": float(total.group(1).replace(",", "")) if total else None,
            }
        if units:
            break
    return list(units.values())


def parse_floorplan_links(html: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"(https?://www\.park35onclairmont\.com)?(/floorplans/birmingham-AL/park-35-on-clairmont-mt/[a-z0-9\-]+-(\d{6,8})-1/)", html):
        out[m.group(3)] = BASE + m.group(2)
    return out


def parse_floorplan_tags(html: str) -> dict[str, list[str]]:
    """Floor plan pages list available units with an amenities/features cell.
    Returns {unit_number: [tags]}. Finds the smallest container that holds
    exactly one unit number and collects known tag phrases inside it."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, list[str]] = {}
    for node in soup.find_all(string=TAG_UNIT_RE):
        el = node.parent
        unit = TAG_UNIT_RE.search(str(node)).group(1)
        best = None
        for _ in range(8):
            if el is None:
                break
            t = el.get_text(" ", strip=True)
            if len(set(m.group(1) for m in TAG_UNIT_RE.finditer(t))) > 1:
                break
            best = t
            el = el.parent
        if not best:
            continue
        tags = []
        for tm in TAG_VOCAB.finditer(best):
            tag = re.sub(r"\s+", " ", tm.group(1)).strip()
            if tag.lower() == "balcony" and re.search(r"juliet\s+balcony", best, re.I) and not re.search(r"(?<!juliet )\bbalcony\b", best.replace("Juliet Balcony", "").replace("juliet balcony", ""), re.I):
                continue
            if tag not in tags:
                tags.append(tag)
        if tags or unit not in result:
            result[unit] = sorted(set(result.get(unit, []) + tags))
    return result


def classify(tags: list[str]) -> tuple[str, str]:
    view = "Unknown"
    for rx, label in VIEW_RULES:
        if any(rx.search(t) for t in tags):
            view = label
            break
    low = [t.lower() for t in tags]
    if any("juliet" in t for t in low) and not any(t in ("balcony", "patio") for t in low):
        porch = "Juliet"
    elif "patio" in low:
        porch = "Patio"
    elif "balcony" in low:
        porch = "Balcony"
    else:
        porch = "Unknown" if not tags else "None"
    if view == "Unknown" and tags:
        view = "Street/exterior (untagged)" if porch in ("Balcony", "Patio") and not any("pool" in t or "wall" in t for t in low) else "Unknown"
    return view, porch


def parse_calculator(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    base = re.search(r"Monthly Base Rent:\s*\$?([\d,]+)", text)
    terms = {}
    for sel in soup.find_all("select"):
        for opt in sel.find_all("option"):
            m = re.match(r"\s*(\d+)\s*months?\s*[—-]\s*\$([\d,]+\.\d{2})", opt.get_text())
            if m:
                terms[int(m.group(1))] = float(m.group(2).replace(",", ""))
    return {"base_rent": float(base.group(1).replace(",", "")) if base else None, "total_by_term": terms}


# ---------- main ----------

def main() -> int:
    today = dt.datetime.now(TZ).date()
    f = Fetcher()
    snapshot = {"date": today.isoformat(), "property": "Park 35", "status": "ok", "units": [], "errors": []}
    try:
        beans = f.get(BEANS_MAP)
        save_raw("beans_map.html", beans)
        if not beans:
            raise RuntimeError("beans map fetch failed (blocked and no proxy result)")
        units = parse_beans_map(beans)
        if not units:
            raise RuntimeError("beans map parsed 0 units; parser or page changed (see raw/beans_map.html)")

        fp_links: dict[str, str] = {}
        for page in FLOORPLAN_INDEX_PAGES:
            h = f.get(page)
            if h:
                fp_links.update(parse_floorplan_links(h))
        save_raw("floorplan_links.json", json.dumps(fp_links, indent=1))

        tags_by_unit: dict[str, list[str]] = {}
        for fp_id in sorted({u["floorplan_id"] for u in units if u["floorplan_id"]}):
            url = fp_links.get(fp_id)
            if not url:
                snapshot["errors"].append(f"no floor plan URL for fp {fp_id}")
                continue
            h = f.get(url)
            save_raw(f"floorplan_{fp_id}.html", h)
            if h:
                tags_by_unit.update(parse_floorplan_tags(h))
            else:
                snapshot["errors"].append(f"floor plan page blocked: {url}")

        for u in units:
            tags = tags_by_unit.get(u["unit"], [])
            u["tags"] = tags
            u["view"], u["porch"] = classify(tags)
            u["top_floor"] = u["floor"] == 5
            ruled_out = u["view"] in ("Pool", "Garage/Wall") or u["porch"] in ("Juliet", "None")
            u["meets_criteria"] = (None if (u["view"] == "Unknown" or u["porch"] == "Unknown")
                                   else (not ruled_out and u["porch"] in ("Balcony", "Patio")))
            u["movein_prices"] = []
            if u["beds"] == 2 and not ruled_out:
                for off in MOVE_IN_OFFSETS_DAYS:
                    d = (today + dt.timedelta(days=off)).isoformat()
                    q = (f"{BASE}/?module=check_availability&is_secure=1&property[id]={PROPERTY_ID}"
                         f"&action=view_rent_calculator&unit_space[id]={u['unit_space_id']}"
                         f"&property_floorplan[id]={u['floorplan_id']}&move_in_date={d}&occupancy_type=conventional")
                    h = f.get(q, xhr=True)
                    if off == MOVE_IN_OFFSETS_DAYS[0]:
                        save_raw(f"calc_{u['unit']}.html", h)
                    if not h:
                        continue
                    c = parse_calculator(h)
                    if c["base_rent"] or c["total_by_term"]:
                        u["movein_prices"].append({"move_in": d, **c})
            if u["movein_prices"]:
                u["base_rent"] = u["movein_prices"][0]["base_rent"]
            snapshot["units"].append(u)
    except SiteDown as e:
        snapshot["status"] = "site_down"
        snapshot["errors"].append(str(e))
    except Exception as e:
        snapshot["status"] = "error"
        snapshot["errors"].append(repr(e))
    snapshot["fetch_stats"] = f.stats

    DATA.mkdir(exist_ok=True)
    (DATA / "latest.json").write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    if snapshot["units"]:
        append_history(snapshot)
    print(json.dumps({"status": snapshot["status"], "units": len(snapshot["units"]),
                      "errors": snapshot["errors"], "stats": f.stats}))
    return 0 if snapshot["status"] in ("ok", "site_down") else 1


def append_history(s: dict) -> None:
    uh = DATA / "units_history.csv"
    new = not uh.exists()
    with uh.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["date", "unit", "unit_space_id", "floorplan", "beds", "sqft", "floor", "available",
                        "view", "porch", "tags", "meets_criteria", "total_monthly_13mo", "base_rent"])
        for u in s["units"]:
            w.writerow([s["date"], u["unit"], u["unit_space_id"], u["floorplan"], u["beds"], u["sqft"], u["floor"],
                        u["available"], u["view"], u["porch"], "|".join(u["tags"]), u["meets_criteria"],
                        u["total_monthly_13mo"], u.get("base_rent")])
    mh = DATA / "movein_history.csv"
    new = not mh.exists()
    with mh.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["date", "unit", "move_in", "base_rent", "lease_months", "total_monthly"])
        for u in s["units"]:
            for mp in u["movein_prices"]:
                for term, total in sorted(mp["total_by_term"].items()) or [(None, None)]:
                    w.writerow([s["date"], u["unit"], mp["move_in"], mp["base_rent"], term, total])


if __name__ == "__main__":
    sys.exit(main())
