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
MOVE_IN_OFFSETS_DAYS = [int(x) for x in os.environ.get("MOVE_IN_OFFSETS", "7,14,30,45,60").split(",")]
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


    # ---- batch mode: one real-browser ScrapingBee session fetches many URLs ----
    BATCH_JS = r"""
(async () => {
  const spec = JSON.parse(atob('%JOBS%'));
  const jobs = (spec.urls || []).map(u => ({url: u}));
  for (const [us, fp] of (spec.calc_units || [])) for (const d of (spec.dates || []))
    jobs.push({url: location.origin + '/?module=check_availability&is_secure=1&property[id]=%PID%&action=view_rent_calculator&unit_space[id]=' + us + '&property_floorplan[id]=' + fp + '&move_in_date=' + d + '&occupancy_type=conventional', xhr: true});
  const out = {};
  if (%MAP%) {
    // Open the Interactive Map tab and read the unit cards from its same-origin iframe.
    try {
      const tab = [...document.querySelectorAll('a,button,li,span,div')]
        .find(e => e.children.length === 0 && /^\s*Interactive Map\s*$/i.test(e.textContent));
      if (tab) tab.click();
      let html = null;
      for (let k = 0; k < 60 && !html; k++) {
        await new Promise(res => setTimeout(res, 500));
        for (const f of document.querySelectorAll('iframe')) {
          try {
            const d = f.contentDocument;
            if (d && d.querySelector('[data-url*="unit_space"]')) { html = d.documentElement.outerHTML; break; }
          } catch (e) {}
        }
      }
      out['__map__'] = {s: html ? 200 : 0, t: html || 'map iframe not found'};
    } catch (e) { out['__map__'] = {s: 0, t: String(e)}; }
  }
  let i = 0;
  async function worker() {
    while (i < jobs.length) {
      const j = jobs[i++];
      try {
        const r = await fetch(j.url, {credentials: 'include',
          headers: j.xhr ? {'X-Requested-With': 'XMLHttpRequest'} : {}});
        out[j.url] = {s: r.status, t: await r.text()};
      } catch (e) { out[j.url] = {s: 0, t: String(e)}; }
      await new Promise(res => setTimeout(res, spec.gap_ms || 400));
    }
  }
  await Promise.all(Array.from({length: spec.workers || 2}, worker));
  const enc = btoa(unescape(encodeURIComponent(JSON.stringify(out))));
  const box = document.createElement('div'); box.id = 'claude-out'; box.textContent = enc;
  document.body.appendChild(box);
  const done = document.createElement('div'); done.id = 'claude-done'; document.body.appendChild(done);
})();
"""

    def batch(self, jobs, start_url: str = BASE + "/", read_map: bool = False) -> dict[str, str]:
        got, _ = self.batch_with_page(jobs, start_url, read_map)
        return got

    def batch_with_page(self, jobs, start_url: str = BASE + "/",
                        read_map: bool = False) -> tuple[dict[str, str], str | None]:
        """Open start_url in ScrapingBee's browser (clears Cloudflare), then fetch
        every job URL from inside the page with the site's own cookies.
        jobs: [{"url": ..., "xhr": bool}]. Returns {url: body} for 200 responses."""
        if not self.sb_key:
            return {}, None
        import base64
        spec = jobs if isinstance(jobs, dict) else {"urls": [j["url"] for j in jobs]}
        js = (self.BATCH_JS.replace("%JOBS%", base64.b64encode(json.dumps(spec, separators=(",", ":")).encode()).decode())
              .replace("%PID%", PROPERTY_ID)
              .replace("%MAP%", "true" if read_map else "false"))
        js = "\n".join(l.strip() for l in js.splitlines() if l.strip() and not l.strip().startswith("//"))
        scenario = {"instructions": [{"wait": 2000}, {"evaluate": js}, {"wait_for": "#claude-done"}]}
        params = {"api_key": self.sb_key, "url": start_url, "render_js": "true", "premium_proxy": "true",
                  "country_code": "us", "block_resources": "false", "timeout": "140000",
                  "js_scenario": json.dumps(scenario)}
        for attempt in range(2):
            try:
                r = creq.get("https://app.scrapingbee.com/api/v1/?" + urlencode(params), timeout=180)
                m = re.search(r'id="claude-out"[^>]*>([A-Za-z0-9+/=\s]+)<', r.text)
                if r.status_code == 200 and m:
                    raw = json.loads(base64.b64decode(re.sub(r"\s", "", m.group(1))).decode("utf-8"))
                    self.stats["proxy_ok"] += 1
                    got = {u: v["t"] for u, v in raw.items() if v.get("s") == 200 and not self._blocked(200, v["t"])}
                    bad = {u: v.get("s") for u, v in raw.items() if u not in got}
                    if bad:
                        print(f"batch: {len(bad)} job(s) failed, statuses {sorted(set(bad.values()))}", file=sys.stderr)
                    page = r.text[:m.start()] if not self._blocked(200, r.text) else None
                    return got, page
                print(f"batch attempt {attempt + 1}: status {r.status_code}, marker={'yes' if m else 'no'}: "
                      f"{r.text[:300]!r}", file=sys.stderr)
                save_raw(f"batch_fail_{attempt + 1}.html", r.text)
            except Exception as e:
                print(f"batch error {e!r}", file=sys.stderr)
        self.stats["proxy_fail"] += 1
        return {}, None


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
            plan = next((l for l in lines[idx + 1:] if not UNIT_RE.search(l) and not re.match(r"(?i)unit number|\d+\s*bed", l)), None)
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
    """Floor plan pages have a unit list table with an 'Amenities' column,
    e.g. 'Special features included with this unit. Golf Course View 7 Balcony'.
    Returns {unit_number: [tags]}; an empty list means the unit has no listed features."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, list[str]] = {}
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        if "unit" not in headers or "amenities" not in headers:
            continue
        iu, ia = headers.index("unit"), headers.index("amenities")
        for tr in table.select("tbody tr"):
            cells = tr.find_all("td", recursive=False)
            if len(cells) <= max(iu, ia):
                continue
            unit = cells[iu].get_text(" ", strip=True).split()[0] if cells[iu].get_text(strip=True) else None
            if not unit:
                continue
            text = cells[ia].get_text(" ", strip=True).replace("Special features included with this unit.", "")
            tags = []
            for tm in TAG_VOCAB.finditer(text):
                tag = re.sub(r"\s+", " ", tm.group(1)).strip()
                if tag not in tags:
                    tags.append(tag)
            # "Balcony" matched inside "Juliet Balcony" is not a real balcony
            if "Balcony" in tags and not re.search(r"(?<!Juliet )\bBalcony\b", text, re.I):
                tags.remove("Balcony")
            result[unit] = tags
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
        porch = "None listed"
    if view == "Unknown":
        view = "No view listed"
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

def calc_url(u: dict, move_in: str) -> str:
    return (f"{BASE}/?module=check_availability&is_secure=1&property[id]={PROPERTY_ID}"
            f"&action=view_rent_calculator&unit_space[id]={u['unit_space_id']}"
            f"&property_floorplan[id]={u['floorplan_id']}&move_in_date={move_in}&occupancy_type=conventional")


def main() -> int:
    today = dt.datetime.now(TZ).date()
    f = Fetcher()
    snapshot = {"date": today.isoformat(), "property": "Park 35", "status": "ok", "mode": None,
                "units": [], "errors": []}

    def get_many(jobs: list[dict]) -> dict[str, str]:
        if snapshot["mode"] == "direct":
            out = {}
            for j in jobs:
                h = f.get(j["url"], xhr=j.get("xhr", False))
                if h:
                    out[j["url"]] = h
            return out
        return f.batch(jobs)

    try:
        # Probe: one direct request decides the mode for the whole run.
        f._wait()
        probe = None
        try:
            r = f.session.get(BEANS_MAP, timeout=45)
            if MAINTENANCE_MARKER in r.text:
                raise SiteDown("Entrata scheduled maintenance")
            if not f._blocked(r.status_code, r.text):
                probe = r.text
        except SiteDown:
            raise
        except Exception as e:
            print(f"probe error {e!r}", file=sys.stderr)
        snapshot["mode"] = "direct" if probe else "browser_batch"
        print(f"mode: {snapshot['mode']}", file=sys.stderr)

        index_jobs = [{"url": u} for u in FLOORPLAN_INDEX_PAGES]
        if probe:
            round1, beans = get_many(index_jobs), probe
        else:
            # The map endpoint only answers inside the site's own iframe, so open the
            # floor plans page, click "Interactive Map", and read the iframe's DOM.
            round1 = f.batch(index_jobs, start_url=FLOORPLAN_INDEX_PAGES[1], read_map=True)
            beans = round1.pop("__map__", None)
        save_raw("beans_map.html", beans)
        if not beans:
            if any(MAINTENANCE_MARKER in v for v in round1.values()):
                raise SiteDown("Entrata scheduled maintenance")
            raise RuntimeError("beans map fetch failed in both direct and browser-batch modes")
        units = parse_beans_map(beans)
        if not units:
            raise RuntimeError("beans map parsed 0 units; parser or page changed (see raw/beans_map.html)")

        fp_links: dict[str, str] = {}
        for h in round1.values():
            fp_links.update(parse_floorplan_links(h))
        save_raw("floorplan_links.json", json.dumps(fp_links, indent=1))

        fp_ids = sorted({u["floorplan_id"] for u in units if u["floorplan_id"]})
        fp_jobs = []
        for fp_id in fp_ids:
            if fp_id in fp_links:
                fp_jobs.append({"url": fp_links[fp_id]})
            else:
                snapshot["errors"].append(f"no floor plan URL for fp {fp_id}")
        move_ins = [(today + dt.timedelta(days=o)).isoformat() for o in MOVE_IN_OFFSETS_DAYS]
        round2 = get_many(fp_jobs) if snapshot["mode"] == "direct" else f.batch(
            {"urls": [j["url"] for j in fp_jobs], "workers": 2, "gap_ms": 600}, start_url=FLOORPLAN_INDEX_PAGES[1])

        tags_by_unit: dict[str, list[str]] = {}
        for j in fp_jobs:
            h = round2.get(j["url"])
            fp_id = re.search(r"-(\d{6,8})-1/", j["url"]).group(1)
            save_raw(f"floorplan_{fp_id}.html", h)
            if h:
                tags_by_unit.update(parse_floorplan_tags(h))
            else:
                snapshot["errors"].append(f"floor plan page failed: {j['url']}")

        for u in units:
            tags = tags_by_unit.get(u["unit"], [])
            u["tags"] = tags
            u["view"], u["porch"] = classify(tags)
            tagged = u["unit"] in tags_by_unit
            ruled_out = u["view"] in ("Pool", "Garage/Wall") or u["porch"] in ("Juliet", "None listed")
            u["meets_criteria"] = (False if (tagged and ruled_out) else
                                   None if not tagged else u["porch"] in ("Balcony", "Patio"))
            u["top_floor"] = u["floor"] == 5 or any(t.lower() == "top floor" for t in tags)
            u["movein_prices"] = []
        # Round 3: rent calculator, one request at a time, only for 2BRs that aren't ruled out.
        calc_targets = [u for u in units if u["beds"] == 2 and u["meets_criteria"] is not False]
        if snapshot["mode"] == "direct":
            round3 = get_many([{"url": calc_url(u, d), "xhr": True} for u in calc_targets for d in move_ins])
        elif calc_targets:
            round3 = f.batch({"calc_units": [[u["unit_space_id"], u["floorplan_id"]] for u in calc_targets],
                              "dates": move_ins, "workers": 1, "gap_ms": 1500}, start_url=FLOORPLAN_INDEX_PAGES[1])
        else:
            round3 = {}
        for u in units:
            if u in calc_targets:
                for d in move_ins:
                    h = round3.get(calc_url(u, d))
                    if d == move_ins[0]:
                        save_raw(f"calc_{u['unit']}.html", h)
                    if not h:
                        continue
                    c = parse_calculator(h)
                    if c["base_rent"] or c["total_by_term"]:
                        u["movein_prices"].append({"move_in": d, **c})
            u["base_rent"] = u["movein_prices"][0]["base_rent"] if u["movein_prices"] else None
            snapshot["units"].append(u)
        n_calc = sum(len(u["movein_prices"]) for u in snapshot["units"])
        if calc_targets and n_calc == 0:
            snapshot["errors"].append("all rent-calculator requests failed")
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
    print(json.dumps({"status": snapshot["status"], "mode": snapshot["mode"], "units": len(snapshot["units"]),
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
