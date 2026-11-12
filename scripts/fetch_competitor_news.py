import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPETITORS_FILE = ROOT / "data" / "competitors.yml"
OUTPUT_FILE = ROOT / "data" / "competitor-news.json"

# Keep the live board focused on recent signals. Older records are removed from
# the live JSON; Git history remains available in the repository.
MAX_AGE_DAYS = 180
MAX_ITEMS = 300

EVENT_KEYWORDS = {
    "acquisitions_and_mergers": [
        "acquisition", "acquired", "acquires", "acquiring", "merger",
        "merged", "merges", "takeover", "buyout", "m&a"
    ],
    "funding_and_investor_rounds": [
        "funding", "fundraise", "fundraising", "funding round", "raised",
        "raises", "investment", "investor", "venture capital", "private equity",
        "series a", "series b", "series c", "series d", "series e", "seed round",
        "strategic investment", "ipo", "valuation"
    ],
    "partnerships_and_integrations": [
        "partnership", "partnered", "partners with", "integration",
        "integrates", "alliance", "partneredge"
    ],
    "product_and_ai_launches": [
        "product launch", "launches a new product", "launched a new product",
        "new feature", "introduces a new", "introduced a new", "ai agent"
    ],
    "customer_wins_and_case_studies": [
        "customer", "case study", "selected", "chosen", "deployed",
        "customer story"
    ],
    "leadership_changes": [
        "appointed", "appoints", "named ceo", "new ceo", "joins as ceo",
        "chief executive", "chief product officer", "chief technology officer",
        "chief marketing officer"
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

# These words can refer to unrelated topics. Require hiring/HR context when
# they are used as the only competitor match.
AMBIGUOUS_BRAND_TERMS = {"sap", "fountain", "workday"}
HIRING_CONTEXT_TERMS = {
    "hiring", "recruiting", "recruitment", "recruiter", "talent",
    "hr tech", "human resources", "candidate", "applicant", "workforce",
    "staffing", "ats", "successfactors", "smartrecruiters", "paradox"
}
UNRELATED_CONTEXT_TERMS = {
    "basketball", "football", "lacrosse", "baseball", "soccer", "hoops",
    "ncaa", "prospects", "golden buzzer", "america's got talent",
    "fountain valley", "fountain hills", "berger fountain", "fountain blues festival"
}

SIGNAL_PATTERNS = {
    "acquisitions_and_mergers": re.compile(
        r"\b(acquires?|acquired|acquiring|buys|bought|merger|merges with|"
        r"takeover|buyout)\b|\bacquisition of\b", re.I
    ),
    "funding_and_investor_rounds": re.compile(
        r"\b(funding round|fundraising|fundraise|seed round|series [a-e] "
        r"(round|funding)|rais(?:e|es|ed)\s+[$€£]?\s?\d|secured\s+[$€£]?\s?\d|"
        r"investment from|invested by)\b", re.I
    ),
    "partnerships_and_integrations": re.compile(
        r"\b(partnership|partnered with|partners with|integration|integrates with|"
        r"alliance)\b", re.I
    ),
    "product_and_ai_launches": re.compile(
        r"\b(launches? (a |an )?(new )?(product|feature|platform|agent)|"
        r"introduces? (a |an )?(new )?(product|feature|platform|agent)|"
        r"unveils? (a |an )?(new )?(product|feature|platform|agent))\b", re.I
    ),
    "leadership_changes": re.compile(
        r"\b(appointed|appoints|named|names|joins as)\b.{0,50}\b(ceo|chief|president|cfo|cto|cmo)\b|"
        r"\b(new ceo|chief executive officer)\b", re.I
    ),
    "geographic_or_segment_expansion": re.compile(
        r"\b(expands into|enters (a |the )?new market|launches in (a |the )?new market|"
        r"opens (a |its first) (office|location|market))\b", re.I
    ),
    "layoffs_and_restructuring": re.compile(
        r"\b(layoffs?|job cuts|workforce reduction|restructuring|shuts down|shutdown)\b", re.I
    ),
}


def clean_html(value):
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_date(value):
    if not value:
        return None

    value = str(value).strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_date(value):
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


def competitor_match_terms(competitor):
    configured = competitor.get("match_terms") or competitor.get("aliases") or []
    if isinstance(configured, str):
        configured = [configured]

    terms = [str(term).strip() for term in configured if str(term).strip()]
    if terms:
        return terms

    competitor_id = str(competitor.get("id", "")).lower()
    name = str(competitor.get("name", "")).strip()

    # "Sense" by itself creates many unrelated search matches. Use Skillate as
    # the default match until an unambiguous Sense-specific alias is configured.
    if "skillate" in competitor_id or "skillate" in name.lower():
        return ["Skillate"]

    # Support combined names such as "SAP / SmartRecruiters".
    return [part.strip() for part in re.split(r"[/|]", name) if part.strip()]


def matches_competitor(competitor, title, description):
    haystack = f"{title} {description}".lower()
    context_found = any(term in haystack for term in HIRING_CONTEXT_TERMS)
    if any(term in haystack for term in UNRELATED_CONTEXT_TERMS):
        return ""

    for term in competitor_match_terms(competitor):
        normalized_term = term.lower().strip()
        if not normalized_term or normalized_term not in haystack:
            continue

        # "Sense" is too ambiguous to match on its own. Keep Skillate monitoring
        # tied to the Skillate name until a distinctive Sense-specific alias is known.
        if normalized_term == "sense":
            continue

        if normalized_term in AMBIGUOUS_BRAND_TERMS and not context_found:
            continue

        # These names also occur frequently in sports, geography, or ordinary
        # language. Require a talent/HR context for a relevant company mention.
        if normalized_term == "phenom" and not (
            "phenom ai" in haystack or context_found
        ):
            continue
        if normalized_term == "eightfold" and not (
            "eightfold ai" in haystack or context_found
        ):
            continue
        if normalized_term == "paradox" and not (
            "workday" in haystack or context_found
        ):
            continue

        if normalized_term == "sap" and not (
            context_found or "workforce startup" in haystack
        ):
            continue

        return term

    return ""


def classify_signal(text):
    matches = [
        signal_type
        for signal_type, pattern in SIGNAL_PATTERNS.items()
        if pattern.search(text or "")
    ]
    return matches or ["general_competitor_news"]


def canonical_url(url):
    if not url:
        return ""

    try:
        parts = urlsplit(url.strip())
        ignored = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "oc"}
        query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                 if key.lower() not in ignored]
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))
    except ValueError:
        return url.strip()


def normalized_title(title):
    title = (title or "").lower()
    title = re.sub(r"https?://\S+", " ", title)
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def read_existing_items():
    if not OUTPUT_FILE.exists():
        return []

    try:
        existing = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        return existing.get("items", [])
    except (json.JSONDecodeError, OSError):
        return []


def fetch_feed(competitor, cutoff):
    url = competitor.get("google_news_rss")
    if not url:
        return [], {"old": 0, "unmatched": 0, "invalid_date": 0}

    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 TurboHirePMMPortfolio/1.0"}
    )

    with urlopen(request, timeout=25) as response:
        xml_data = response.read()

    root = ET.fromstring(xml_data)
    items = []
    skipped = {"old": 0, "unmatched": 0, "invalid_date": 0}

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

        published_date = parse_date(published_at)
        if not published_date:
            skipped["invalid_date"] += 1
            continue
        if published_date < cutoff:
            skipped["old"] += 1
            continue

        matched_term = matches_competitor(competitor, title, description)
        if not matched_term:
            skipped["unmatched"] += 1
            continue

        identity = canonical_url(link) or title
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
            "review_status": "pending",
            "matched_term": matched_term
        })

    return items, skipped


def merge_preserving_review(old_item, new_item, fetched_at):
    if not old_item:
        new_item["first_seen_at"] = fetched_at
        return new_item

    merged = dict(old_item)
    merged.update(new_item)

    # Do not reset a human's review work every time the workflow refreshes.
    for key in ("verification_status", "review_status", "first_seen_at", "notes", "agent_summary"):
        if key in old_item:
            merged[key] = old_item[key]

    merged["last_seen_at"] = fetched_at
    return merged


def deduplicate(items):
    # Newest items are retained first. Exact/canonical URL duplicates and very
    # similar headlines from the same competitor are combined. Semantically
    # similar but differently worded stories may still need human review.
    items = sorted(
        items,
        key=lambda item: item.get("published_at", ""),
        reverse=True
    )
    result = []
    by_url = {}
    by_exact_title = {}

    for item in items:
        comp_id = item.get("competitor_id", "")
        url_key = canonical_url(item.get("url", ""))
        title_key = (comp_id, normalized_title(item.get("title", "")))

        duplicate_index = by_url.get(url_key) if url_key else None
        if duplicate_index is None:
            duplicate_index = by_exact_title.get(title_key)

        if duplicate_index is None:
            date = parse_date(item.get("published_at", ""))
            title = normalized_title(item.get("title", ""))
            if date and title:
                for idx, existing in enumerate(result):
                    if existing.get("competitor_id", "") != comp_id:
                        continue
                    other_date = parse_date(existing.get("published_at", ""))
                    other_title = normalized_title(existing.get("title", ""))
                    if not other_date or abs((date - other_date).days) > 10:
                        continue
                    if SequenceMatcher(None, title, other_title).ratio() >= 0.92:
                        duplicate_index = idx
                        break

        if duplicate_index is None:
            idx = len(result)
            result.append(item)
            if url_key:
                by_url[url_key] = idx
            by_exact_title[title_key] = idx
            continue

        primary = result[duplicate_index]
        extra_sources = list(primary.get("additional_sources", []))
        alt = {
            "title": item.get("title", ""),
            "publisher": item.get("publisher", ""),
            "url": item.get("url", "")
        }
        if alt["url"] and alt["url"] != primary.get("url") and alt not in extra_sources:
            extra_sources.append(alt)
        if extra_sources:
            primary["additional_sources"] = extra_sources

    return result


def main():
    if not COMPETITORS_FILE.exists():
        raise FileNotFoundError(
            f"Could not find {COMPETITORS_FILE.relative_to(ROOT)}"
        )

    config = yaml.safe_load(COMPETITORS_FILE.read_text(encoding="utf-8")) or {}
    competitors = [
        competitor for competitor in config.get("competitors", [])
        if competitor.get("enabled", True)
    ]
    competitors_by_id = {
        str(competitor.get("id", "")): competitor
        for competitor in competitors
    }

    fetched_at = datetime.now(timezone.utc).isoformat()
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    existing_items = read_existing_items()

    # Retain only recent, dated records from the previous JSON. This removes
    # stale historical noise that would otherwise remain forever.
    records = {}
    dropped_old = 0
    dropped_invalid_date = 0
    dropped_unmatched_existing = 0
    for item in existing_items:
        published_date = parse_date(item.get("published_at", ""))
        if not published_date:
            dropped_invalid_date += 1
            continue
        if published_date < cutoff:
            dropped_old += 1
            continue
        competitor = competitors_by_id.get(str(item.get("competitor_id", "")))
        if competitor and not matches_competitor(
            competitor,
            item.get("title", ""),
            item.get("summary", "")
        ):
            dropped_unmatched_existing += 1
            continue
        key = canonical_url(item.get("url", "")) or item.get("id", "")
        if key:
            records[key] = item

    errors = []
    successful_feeds = 0
    skipped_totals = {"old": 0, "unmatched": 0, "invalid_date": 0}

    for competitor in competitors:
        try:
            new_items, skipped = fetch_feed(competitor, cutoff)
            successful_feeds += 1

            for key in skipped_totals:
                skipped_totals[key] += skipped.get(key, 0)

            for item in new_items:
                key = canonical_url(item.get("url", "")) or item.get("id", "")
                old_item = records.get(key)
                records[key] = merge_preserving_review(old_item, item, fetched_at)

            name = competitor.get("name", "Unknown competitor")
            print(
                f"Fetched {len(new_items)} recent matches for {name}; "
                f"skipped {skipped['old']} old, {skipped['unmatched']} unmatched, "
                f"{skipped['invalid_date']} with invalid dates"
            )

        except Exception as error:
            name = competitor.get("name", "Unknown competitor")
            errors.append({"competitor": name, "error": str(error)})
            print(f"Feed error for {name}: {error}", file=sys.stderr)

    if successful_feeds == 0:
        raise RuntimeError("No competitor feeds could be read.")

    all_items = deduplicate(list(records.values()))
    all_items.sort(
        key=lambda item: item.get("published_at", ""),
        reverse=True
    )

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "refreshed_at": fetched_at,
        "source_note": (
            f"Google News RSS is used for discovery. Items older than {MAX_AGE_DAYS} "
            "days and results without a configured competitor-name match are excluded. "
            "Verify important signals through an official source or credible publication."
        ),
        "feed_errors": errors,
        "filter_summary": {
            "retention_days": MAX_AGE_DAYS,
            "dropped_old_existing_items": dropped_old,
            "dropped_existing_items_without_valid_dates": dropped_invalid_date,
            "dropped_unmatched_existing_items": dropped_unmatched_existing,
            "skipped_old_feed_items": skipped_totals["old"],
            "skipped_unmatched_feed_items": skipped_totals["unmatched"],
            "skipped_feed_items_with_invalid_dates": skipped_totals["invalid_date"]
        },
        "items": all_items[:MAX_ITEMS]
    }

    OUTPUT_FILE.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )

    print(f"Wrote {min(len(all_items), MAX_ITEMS)} items to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
