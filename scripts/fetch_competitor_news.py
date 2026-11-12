import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPETITORS_FILE = ROOT / "data" / "competitors.yml"
OUTPUT_FILE = ROOT / "data" / "competitor-news.json"
MAX_ITEMS = 500

EVENT_KEYWORDS = {
    "acquisitions_and_mergers": [
        "acquisition", "acquired", "acquires", "merger", "merged",
        "merges", "takeover", "buyout"
    ],
    "funding_and_investor_rounds": [
        "funding", "fundraise", "fundraising", "raised", "investment",
        "investor", "series a", "series b", "series c", "seed round"
    ],
    "partnerships_and_integrations": [
        "partnership", "partnered", "partners with", "integration",
        "integrates", "alliance"
    ],
    "product_and_ai_launches": [
        "product launch", "launches", "launched", "announces", "announced",
        "introduces", "introduced", "new product", "ai agent"
    ],
    "customer_wins_and_case_studies": [
        "customer", "case study", "selected", "chosen", "deployed",
        "customer story"
    ],
    "leadership_changes": [
        "appointed", "appoints", "named ceo", "new ceo", "joins as ceo",
        "chief executive"
    ],
    "geographic_or_segment_expansion": [
        "expansion", "expands", "enters", "new market", "launches in"
    ],
    "regulatory_security_and_compliance": [
        "regulation", "regulatory", "compliance", "security",
        "data privacy", "certification"
    ],
    "layoffs_and_restructuring": [
        "layoffs", "layoff", "restructuring", "job cuts", "workforce reduction"
    ],
}


def clean_html(value):
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_date(value):
    if not value:
        return ""

    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return value


def classify_signal(text):
    text = text.lower()
    matches = []

    for signal_type, keywords in EVENT_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            matches.append(signal_type)

    return matches or ["general_competitor_news"]


def read_existing_items():
    if not OUTPUT_FILE.exists():
        return []

    try:
        existing = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        return existing.get("items", [])
    except (json.JSONDecodeError, OSError):
        return []


def fetch_feed(competitor):
    url = competitor.get("google_news_rss")
    if not url:
        return []

    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 TurboHirePMMPortfolio/1.0"}
    )

    with urlopen(request, timeout=25) as response:
        xml_data = response.read()

    root = ET.fromstring(xml_data)
    items = []

    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = clean_html(item.findtext("description") or "")
        published_at = normalize_date(item.findtext("pubDate") or "")

        source_element = item.find("source")
        publisher = ""
        if source_element is not None and source_element.text:
            publisher = source_element.text.strip()

        if not title or not link:
            continue

        identity = link or title
        record_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]

        items.append({
            "id": record_id,
            "competitor_id": competitor.get("id", ""),
            "competitor": competitor.get("name", ""),
            "group": competitor.get("group", ""),
            "title": title,
            "summary": description[:700],
            "url": link,
            "publisher": publisher or "Google News",
            "published_at": published_at,
            "signal_types": classify_signal(f"{title} {description}"),
            "source_type": "Google News RSS discovery",
            "verification_status": "unverified",
            "review_status": "pending"
        })

    return items


def main():
    if not COMPETITORS_FILE.exists():
        raise FileNotFoundError(
            f"Could not find {COMPETITORS_FILE.relative_to(ROOT)}"
        )

    config = yaml.safe_load(COMPETITORS_FILE.read_text(encoding="utf-8")) or {}
    competitors = config.get("competitors", [])

    existing_items = read_existing_items()
    records = {
        item.get("url") or item.get("id"): item
        for item in existing_items
    }

    errors = []
    successful_feeds = 0
    fetched_at = datetime.now(timezone.utc).isoformat()

    for competitor in competitors:
        try:
            new_items = fetch_feed(competitor)
            successful_feeds += 1

            for item in new_items:
                item["first_seen_at"] = fetched_at
                records[item["url"]] = item

            print(f"Fetched {len(new_items)} items for {competitor.get('name')}")

        except Exception as error:
            name = competitor.get("name", "Unknown competitor")
            errors.append({"competitor": name, "error": str(error)})
            print(f"Feed error for {name}: {error}", file=sys.stderr)

    if successful_feeds == 0:
        raise RuntimeError("No competitor feeds could be read.")

    all_items = list(records.values())
    all_items.sort(
        key=lambda item: item.get("published_at", ""),
        reverse=True
    )

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "refreshed_at": fetched_at,
        "source_note": (
            "Google News RSS is used for discovery. Verify important "
            "signals through an official source or credible publication."
        ),
        "feed_errors": errors,
        "items": all_items[:MAX_ITEMS]
    }

    OUTPUT_FILE.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )

    print(f"Wrote {min(len(all_items), MAX_ITEMS)} items to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
