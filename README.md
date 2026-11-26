# apartment-tracker

Daily price tracking for Park 35 on Clairmont (Birmingham, AL).

A GitHub Actions job runs every morning at 6:17 AM Central. It reads Park 35's own Entrata site, which includes:
- every available unit
- each unit's view and porch tags from its floor plan page
- the rent calculator's prices by move-in date and lease length, for 2BR units that aren't ruled out

The job commits the results to `data/`. A Claude scheduled task then copies them into the Google Sheet "Apartment Price Tracker" and sends the daily alert.

- `data/latest.json`: today's full snapshot
- `data/units_history.csv`: one row per unit per day
- `data/movein_history.csv`: one row per unit, move-in date and lease length per day
- `raw/`: raw HTML from the latest run, kept for fixing the parser

Each request goes directly to Park 35's site first. If Cloudflare blocks it, the job retries through ScrapingBee with its premium proxy, but only when the repo secret `SCRAPINGBEE_API_KEY` is set. Requests go out one at a time, 4 seconds apart, because the site rate-limits bursts.

Run it by hand from Actions → "Daily Park 35 scrape" → Run workflow.
