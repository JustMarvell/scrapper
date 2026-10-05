#!/usr/bin/env python3
"""
Steam data collector for an indie-game Decision Support System.

Criteria collected
  C1.1 positive_ratio_pct      C1.2 total_reviews
  C2.1 base_price_usd          C2.2 price_per_hour_usd
  C3.1 avg_playtime_hours      C3.2 median_playtime_hours
  C4.1 owners_mid (+ range)    C4.2 peak_ccu

Sources
  - SteamSpy API         -> game list by tag, owners, playtime, ccu
  - Steam appreviews API -> review counts / positive ratio
  - Steam appdetails API -> base (initial) price in USD
  - SteamCharts (opt.)   -> all-time peak concurrent players

Install:  pip install requests pandas
Usage:
  python steam_dss_scraper.py --tag Indie --limit 100 --out games.csv
  python steam_dss_scraper.py --appids ids.txt --steamcharts --out games.csv
  (ids.txt = one Steam appid per line)

Notes
  - SteamSpy owners are ranges (e.g. "20,000 .. 50,000"); midpoint is used.
  - SteamSpy 'ccu' = peak concurrent players yesterday. Use --steamcharts for
    the all-time peak instead (slower, scrapes HTML).
  - Free games are kept but price_per_hour is left empty (price = 0).
  - Progress is saved to the CSV every 10 games, and already-scraped appids
    are skipped on re-run, so you can stop and resume.
"""
import argparse
import os
import re
import statistics
import time

import pandas as pd
import requests

VERSION = "v3 (Steam store search + tag check)"
HEADERS = {"User-Agent": "Mozilla/5.0 (academic DSS research project)"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def get_json(url, params=None, retries=4, wait=5):
    """GET with simple retry/backoff. Returns parsed JSON or None."""
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=20)
            if r.status_code == 429:  # rate limited
                time.sleep(wait * (attempt + 1) * 2)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            time.sleep(wait * (attempt + 1))
    return None


# Game List
FALLBACK_TAG_IDS = {
    "indie": 492, "action": 19, "adventure": 21, "rpg": 122, "strategy": 9,
    "simulation": 599, "horror": 1667, "puzzle": 1664, "casual": 597,
    "roguelike": 1716, "platformer": 1625, "survival": 1662,
}


def resolve_tag_id(tag):
    """Tag name (or numeric id) -> Steam tag id."""
    if str(tag).isdigit():
        return int(tag)
    data = get_json("https://store.steampowered.com/tagdata/populartags/english")
    if isinstance(data, list):
        for t in data:
            if t.get("name", "").lower() == tag.lower():
                return int(t["tagid"])
    return FALLBACK_TAG_IDS.get(tag.lower())


def list_appids_by_tag(tag, limit, sort_by="Reviews_DESC"):
    """
    Game list from Steam store search, filtered by tag id.
    category1=998 restricts results to games (no DLC/software/soundtracks).
    sort_by: Reviews_DESC (most reviewed), Released_DESC (newest), or
             '_ASC' (relevance).
    """
    tag_id = resolve_tag_id(tag)
    if tag_id is None:
        raise SystemExit(f"Unknown tag '{tag}'. Pass its numeric Steam tag id "
                         f"instead (see the store's tag URLs, e.g. ?tags=1667).")
    print(f"Tag '{tag}' -> id {tag_id}")
    ids, start, page = [], 0, 50
    while len(ids) < limit:
        data = get_json("https://store.steampowered.com/search/results/", {
            "query": "", "start": start, "count": page, "tags": tag_id,
            "category1": 998, "sort_by": sort_by, "infinite": 1,
            "cc": "us", "l": "english"})
        if not data or not data.get("results_html"):
            break
        found = re.findall(r'data-ds-appid="(\d+)"', data["results_html"])
        if not found:
            break
        for a in found:
            a = int(a)
            if a not in ids:  # bundles can list several ids; ignore dupes
                ids.append(a)
        start += page
        time.sleep(1.5)
    return ids[:limit]


# Per source fetcher
def steamspy_details(appid):
    time.sleep(1.0)  # SteamSpy: ~1 request/second
    return get_json("https://steamspy.com/api.php",
                    {"request": "appdetails", "appid": appid})


def steam_reviews(appid):
    data = get_json(
        f"https://store.steampowered.com/appreviews/{appid}",
        {"json": 1, "language": "all", "purchase_type": "all",
         "num_per_page": 0})
    if not data or data.get("success") != 1:
        return None
    return data.get("query_summary", {})


def steam_price(appid):
    """Base (pre-discount) price in USD, plus name and release date."""
    data = get_json("https://store.steampowered.com/api/appdetails",
                    {"appids": appid, "cc": "us", "l": "english"})
    if not data:
        return None
    entry = data.get(str(appid), {})
    if not entry.get("success"):
        return None
    d = entry["data"]
    if d.get("is_free"):
        price = 0.0
    else:
        po = d.get("price_overview")
        price = po["initial"] / 100 if po else None
    return {"name": d.get("name"), "type": d.get("type"),
            "release_date": d.get("release_date", {}).get("date"),
            "base_price_usd": price}


def steamcharts_peak(appid):
    """All-time peak concurrent players, scraped from SteamCharts."""
    try:
        time.sleep(1.5)
        r = SESSION.get(f"https://steamcharts.com/app/{appid}", timeout=20)
        if r.status_code != 200:
            return None
        nums = re.findall(r'<span class="num">\s*([\d,]+)\s*</span>', r.text)
        # order on page: now, 24h peak, all-time peak
        if len(nums) >= 3:
            return int(nums[2].replace(",", ""))
    except requests.RequestException:
        pass
    return None


def review_playtime_sample(appid, sample_size=300):
    """
    Fallback when SteamSpy has no playtime: read up to `sample_size` recent
    reviews from Steam and use the reviewers' lifetime playtime.
    Returns (avg_hours, median_hours, n) or None.
    Caveat: reviewers usually play more than the average owner, so this
    over-estimates true playtime. Keep it consistent across all games.
    """
    minutes, cursor, seen = [], "*", set()
    while len(minutes) < sample_size:
        data = get_json(
            f"https://store.steampowered.com/appreviews/{appid}",
            {"json": 1, "language": "all", "purchase_type": "all",
             "filter": "recent", "num_per_page": 100, "cursor": cursor})
        if not data or data.get("success") != 1:
            break
        reviews = data.get("reviews", [])
        new_cursor = data.get("cursor")
        if not reviews or not new_cursor or new_cursor in seen:
            break
        seen.add(new_cursor)
        cursor = new_cursor
        minutes += [r["author"]["playtime_forever"] for r in reviews
                    if "playtime_forever" in r.get("author", {})]
        time.sleep(0.5)
    minutes = minutes[:sample_size]
    if not minutes:
        return None
    return (round(statistics.mean(minutes) / 60, 2),
            round(statistics.median(minutes) / 60, 2),
            len(minutes))


def parse_owners(s):
    """'20,000 .. 50,000' -> (20000, 50000, 35000)"""
    try:
        lo, hi = [int(x.replace(",", "").strip()) for x in s.split("..")]
        return lo, hi, (lo + hi) // 2
    except Exception:
        return None, None, None


# main record builder
def collect(appid, use_steamcharts, review_sample=300, required_tag=None):
    spy = steamspy_details(appid)
    if not spy:
        return None

    # Safety check: SteamSpy lists each game's user tags ({tag: votes}).
    # If we have tag data and the wanted tag is missing, skip the game.
    tags = spy.get("tags")
    tags = tags if isinstance(tags, dict) else {}
    if required_tag and tags and required_tag.lower() not in (
            t.lower() for t in tags):
        return None
    top_tags = ", ".join(list(tags)[:5])

    rev = steam_reviews(appid)
    store = steam_price(appid)
    if not (rev and store):
        return None
    if store.get("type") != "game":
        return None

    pos, neg = rev.get("total_positive", 0), rev.get("total_negative", 0)
    total = pos + neg
    lo, hi, mid = parse_owners(spy.get("owners", ""))

    avg_h = (spy.get("average_forever") or 0) / 60
    med_h = (spy.get("median_forever") or 0) / 60
    playtime_source = "steamspy"
    if avg_h <= 0 and review_sample > 0:
        sample = review_playtime_sample(appid, review_sample)
        if sample:
            avg_h, med_h, n = sample
            playtime_source = f"review_sample_n{n}"
        else:
            playtime_source = "unavailable"
    elif avg_h <= 0:
        playtime_source = "unavailable"
    price = store["base_price_usd"]

    peak = steamcharts_peak(appid) if use_steamcharts else None
    if peak is None:
        peak = spy.get("ccu")  # fallback: yesterday's peak

    return {
        "appid": appid,
        "name": store["name"],
        "search_tag": required_tag,
        "top_tags": top_tags,
        "release_date": store["release_date"],
        "positive_ratio_pct": round(100 * pos / total, 2) if total else None,  # C1.1
        "total_reviews": total,                                                 # C1.2
        "base_price_usd": price,                                                # C2.1
        "price_per_hour_usd": (round(price / avg_h, 4)
                               if price and avg_h > 0 else None),               # C2.2
        "avg_playtime_hours": round(avg_h, 2) if avg_h > 0 else None,           # C3.1
        "median_playtime_hours": round(med_h, 2) if med_h > 0 else None,        # C3.2
        "playtime_source": playtime_source,
        "owners_range": spy.get("owners"),                                      # C4.1
        "owners_min": lo, "owners_max": hi, "owners_mid": mid,
        "peak_ccu": peak,                                                       # C4.2
        "peak_ccu_source": "steamcharts_alltime" if (use_steamcharts and peak
                           and peak != spy.get("ccu")) else "steamspy_yesterday",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="Indie", help="SteamSpy tag, e.g. Indie, Roguelike")
    ap.add_argument("--limit", type=int, default=100, help="max games to scrape")
    ap.add_argument("--appids", help="text file with one appid per line (overrides --tag)")
    ap.add_argument("--min-reviews", type=int, default=0, help="drop games below this")
    ap.add_argument("--steamcharts", action="store_true",
                    help="use all-time peak CCU from SteamCharts")
    ap.add_argument("--review-sample", type=int, default=300,
                    help="reviews to sample for playtime when SteamSpy has none "
                         "(0 = disable fallback)")
    ap.add_argument("--sort", default="Reviews_DESC",
                    choices=["Reviews_DESC", "Released_DESC", "_ASC"],
                    help="order of the store search (most reviewed / newest / relevance)")
    ap.add_argument("--no-tag-check", action="store_true",
                    help="don't skip games missing the tag in SteamSpy's tag data")
    ap.add_argument("--out", default="steam_games.csv")
    args = ap.parse_args()

    print(f"=== steam_dss_scraper {VERSION} ===")

    # Refuse to resume from a file made by an older version / another tag.
    if os.path.exists(args.out):
        old_cols = pd.read_csv(args.out, nrows=1).columns
        if "search_tag" not in old_cols:
            raise SystemExit(
                f"'{args.out}' was made by an older version of this script "
                f"(no 'search_tag' column). Delete it or use a different --out.")
        old_tags = set(pd.read_csv(args.out)["search_tag"].dropna())
        if old_tags and not args.appids and args.tag not in old_tags:
            raise SystemExit(
                f"'{args.out}' contains tag(s) {old_tags}, but you asked for "
                f"'{args.tag}'. Use a different --out file per tag.")

    if args.appids:
        with open(args.appids) as f:
            ids = [int(x.strip()) for x in f if x.strip().isdigit()]
    else:
        print(f"Fetching game list for tag '{args.tag}' ...")
        ids = list_appids_by_tag(args.tag, args.limit, args.sort)
    if not ids:
        raise SystemExit(
            "No appids returned (Steam search failed or was rate-limited). "
            "Nothing was written. Wait a few minutes and try again.")
    print(f"{len(ids)} appids to process. First 5: {ids[:5]}")

    rows, done = [], set()
    if os.path.exists(args.out):
        old = pd.read_csv(args.out)
        rows = old.to_dict("records")
        done = set(old["appid"])
        print(f"Resuming: {len(done)} games already in {args.out}")

    for i, appid in enumerate(ids, 1):
        if appid in done:
            continue
        required = None if (args.appids or args.no_tag_check) else args.tag
        rec = collect(appid, args.steamcharts, args.review_sample, required)
        if rec is not None and not required:
            rec["search_tag"] = None if args.appids else args.tag
        if rec and rec["total_reviews"] >= args.min_reviews:
            rows.append(rec)
            print(f"[{i}/{len(ids)}] {rec['name']}  "
                  f"{rec['positive_ratio_pct']}% of {rec['total_reviews']} reviews")
        else:
            print(f"[{i}/{len(ids)}] skipped {appid}")
        if i % 10 == 0:
            pd.DataFrame(rows).to_csv(args.out, index=False)

    pd.DataFrame(rows).to_csv(args.out, index=False)
    print(f"Done. {len(rows)} games saved to {args.out}")


if __name__ == "__main__":
    main()