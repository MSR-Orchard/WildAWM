#!/usr/bin/env python3
"""Copy a world's webharbor/<dbfile> seeds into the in-image WebHarbor sites."""
import shutil
import sys
from pathlib import Path


SITE_BY_DBFILE = {
    "allrecipes.db": "allrecipes",
    "amazon_store.db": "amazon",
    "apple_store.db": "apple",
    "arxiv.db": "arxiv",
    "bbc_news.db": "bbc_news",
    "booking.db": "booking",
    "github_mirror.db": "github",
    "google_flights.db": "google_flights",
    "gmaps.db": "google_map",
    "google_search.db": "google_search",
    "hf.db": "huggingface",
    "wolfram_alpha.db": "wolfram_alpha",
    "cambridge.db": "cambridge_dictionary",
    "coursera.db": "coursera",
    "espn.db": "espn",
}
WEBSYN = Path("/opt/WebSyn")
def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: load_webharbor_seeds.py <webharbor_dir>")
        return 2
    whdir = Path(args[0])
    if not whdir.is_dir():
        print(f"[load_seeds] no webharbor dir at {whdir}; nothing to load")
        return 0

    loaded = []
    for db in sorted(whdir.glob("*.db")):
        site = SITE_BY_DBFILE.get(db.name)
        if not site:
            print(f"[load_seeds] WARN: unknown db {db.name}, skipping")
            continue
        seed_dir = WEBSYN / site / "instance_seed"
        if not seed_dir.is_dir():
            print(f"[load_seeds] WARN: no instance_seed for {site}")
            continue
        shutil.copy2(db, seed_dir / db.name)
        loaded.append(site)
        print(f"[load_seeds] {db.name} -> /opt/WebSyn/{site}/instance_seed/")

    print(f"[load_seeds] loaded {len(loaded)} site seeds: {', '.join(loaded)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
