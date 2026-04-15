#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup



DEFAULT_QUERY = "gucci バッグ"
DEFAULT_MIN_PRICE_YEN = 3000
DEFAULT_MAX_PRICE_YEN = 20000
DEFAULT_MIN_SCORE = 2
DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_EXCLUDED_KEYWORDS = ("まとめ売り", "セット", "ジャンク", "大量", "PRADA", "FENDI", "セリーヌ", "ヴィトン")
DEFAULT_PRIORITY_KEYWORDS = ("極美品", "美品")
SCORE_KEYWORDS = {
    "極美品": 4,
    "超美品": 3,
    "美品": 2,
    "未使用": 4,
    "新品": 4,
    "良品": 1,
    "本物保証": 2,
    "中古": -1,
    "訳あり": -3,
    "難あり": -3,
    "汚れ": -2,
    "破れ": -3,
    "破損": -3,
    "ジャンク": -6,
}
MAX_TITLE_LENGTH = 58
DEFAULT_STATE_FILE = ".yahoo_auction_line_alert_state.json"
BAG_TYPE_KEYWORDS = (
    ("ショルダー", ("ショルダー", "クロスボディ", "斜め掛け", "肩掛け")),
    ("トート", ("トート",)),
    ("ハンド", ("ハンド",)),
    ("ボディ", ("ボディ", "ウエスト")),
    ("クラッチ", ("クラッチ", "セカンド")),
    ("ポーチ", ("ポーチ",)),
    ("リュック", ("リュック", "バックパック")),
    ("ボストン", ("ボストン",)),
)
CONDITION_KEYWORDS = ("極美品", "超美品", "美品", "未使用", "新品", "良品", "中古", "訳あり", "難あり", "ジャンク")
YAHOO_AUCTIONS_SEARCH_URL = "https://auctions.yahoo.co.jp/search/search"
DISCORD_MESSAGE_LIMIT = 2000


@dataclass(frozen=True)
class AuctionItem:
    auction_id: str
    title: str
    price_yen: int
    url: str


def yen(value: int) -> str:
    return f"¥{value:,}"


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_auction_id(url: str) -> str | None:
    match = re.search(r"/auction/([A-Za-z0-9]+)", url)
    return match.group(1) if match else None


def extract_prices(text: str) -> list[int]:
    prices: list[int] = []
    normalized = clean_text(text)
    for match in re.finditer(r"(?<![0-9])([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\s*円", normalized):
        start = max(0, match.start() - 12)
        context = normalized[start : match.start()]
        if any(word in context for word in ("送料", "配送", "手数料")):
            continue
        try:
            prices.append(int(match.group(1).replace(",", "")))
        except ValueError:
            continue
    return prices


def title_from_anchor(anchor) -> str:
    for attr in ("aria-label", "title"):
        value = anchor.get(attr)
        if value:
            return clean_text(value)
    image = anchor.find("img", alt=True)
    if image and image.get("alt"):
        return clean_text(image["alt"])
    return clean_text(anchor.get_text(" "))


def title_score(title: str) -> int:
    lowered = title.lower()
    if lowered in {"new!!", "new!", "new"}:
        return 0
    score = len(title)
    if "gucci" in lowered or "グッチ" in title:
        score += 100
    if "バッグ" in title or "バック" in title or "bag" in lowered:
        score += 50
    return score


def priority_score(title: str, priority_keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in priority_keywords if keyword in title)


def keyword_score(title: str) -> tuple[int, list[str]]:
    matched_keywords: list[str] = []
    matched_ranges: list[range] = []
    score = 0
    for keyword, points in sorted(SCORE_KEYWORDS.items(), key=lambda item: len(item[0]), reverse=True):
        start = title.find(keyword)
        while start != -1:
            current_range = range(start, start + len(keyword))
            if not any(start in matched_range or start + len(keyword) - 1 in matched_range for matched_range in matched_ranges):
                matched_ranges.append(current_range)
                matched_keywords.append(f"{keyword}{points:+d}")
                score += points
                break
            start = title.find(keyword, start + 1)
    return score, matched_keywords


def shorten_title(title: str, max_length: int = MAX_TITLE_LENGTH) -> str:
    title = clean_text(title)
    if len(title) <= max_length:
        return title
    return title[: max_length - 1].rstrip() + "…"


def extract_condition(title: str) -> str:
    for keyword in CONDITION_KEYWORDS:
        if keyword in title:
            return keyword
    return ""


def extract_bag_types(title: str) -> list[str]:
    bag_types: list[str] = []
    for label, keywords in BAG_TYPE_KEYWORDS:
        if any(keyword in title for keyword in keywords):
            bag_types.append(label)
    return bag_types


def compact_item_summary(title: str) -> str:
    parts = []
    condition = extract_condition(title)
    bag_types = extract_bag_types(title)
    if bag_types:
        parts.append("/".join(bag_types[:2]))
    if condition:
        parts.append(condition)
    return " ".join(parts) if parts else "バッグ"


def has_excluded_keyword(title: str, excluded_keywords: tuple[str, ...]) -> bool:
    lowered_title = title.lower()
    return any(keyword.lower() in lowered_title for keyword in excluded_keywords)


def candidate_text_blocks(anchor) -> Iterable[str]:
    for parent in [anchor, *anchor.parents]:
        if getattr(parent, "name", None) in ("body", "html", "[document]"):
            break
        text = clean_text(parent.get_text(" "))
        if "円" in text:
            yield text


def parse_auction_items(
    html: str,
    min_price_yen: int,
    max_price_yen: int,
    excluded_keywords: tuple[str, ...],
    priority_keywords: tuple[str, ...],
    min_score: int,
) -> list[AuctionItem]:
    soup = BeautifulSoup(html, "html.parser")
    items_by_id: dict[str, AuctionItem] = {}

    for anchor in soup.find_all("a", href=True):
        url = anchor["href"]
        auction_id = extract_auction_id(url)
        if not auction_id:
            continue

        title = title_from_anchor(anchor)
        if not title or title_score(title) == 0:
            continue
        if has_excluded_keyword(title, excluded_keywords):
            continue

        prices: list[int] = []
        for block in candidate_text_blocks(anchor):
            prices = extract_prices(block)
            if prices:
                break

        if not prices:
            continue

        price_yen = min(prices)
        score, _ = keyword_score(title)
        if min_price_yen <= price_yen <= max_price_yen and score >= min_score:
            item = AuctionItem(
                auction_id=auction_id,
                title=title,
                price_yen=price_yen,
                url=url,
            )
            existing = items_by_id.get(auction_id)
            if existing is None or title_score(item.title) > title_score(existing.title):
                items_by_id[auction_id] = item

    return sorted(
        items_by_id.values(),
        key=lambda item: (-priority_score(item.title, priority_keywords), item.price_yen),
    )


def fetch_search_results(query: str) -> str:
    params = {
        "p": query,
        "va": query,
        "exflg": "1",
        "b": "1",
        "n": "50",
        "s1": "end",
        "o1": "a",
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    response = requests.get(YAHOO_AUCTIONS_SEARCH_URL, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def load_notified_ids(state_file: Path) -> set[str]:
    if not state_file.exists():
        return set()
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if isinstance(data, dict) and isinstance(data.get("notified_ids"), list):
        return {str(item) for item in data["notified_ids"]}
    return set()


def save_notified_ids(state_file: Path, notified_ids: set[str]) -> None:
    state_file.write_text(
        json.dumps({"notified_ids": sorted(notified_ids)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_message(
    query: str,
    min_price_yen: int,
    max_price_yen: int,
    min_score: int,
    items: list[AuctionItem],
    max_chars: int = DISCORD_MESSAGE_LIMIT,
) -> str:
    lines = [
        f"Yahoo Auctions: {query} | {len(items)} items | {yen(min_price_yen)}-{yen(max_price_yen)} | Score >= +{min_score}",
    ]
    for item in items[:10]:
        score, _ = keyword_score(item.title)
        lines.extend(
            [
                f"{yen(item.price_yen)} | Score {score:+d} | {compact_item_summary(item.title)}",
                item.url,
            ]
        )
    if len(items) > 10:
        search_url = f"{YAHOO_AUCTIONS_SEARCH_URL}?p={quote_plus(query)}"
        lines.append(f"+{len(items) - 10} more | {search_url}")
    message = "\n".join(lines).strip()
    if len(message) <= max_chars:
        return message
    suffix = "\n\nMessage truncated. Open Yahoo Auctions search for more results."
    return message[: max_chars - len(suffix)].rstrip() + suffix


def send_discord_message(message: str) -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")
    response = requests.post(
        webhook_url,
        json={
            "content": message,
            "allowed_mentions": {"parse": []},
        },
        timeout=30,
    )
    response.raise_for_status()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Yahoo Auctions and send Discord alerts for cheap listings.")
    parser.add_argument("--query", default=os.getenv("YAHOO_AUCTIONS_QUERY", DEFAULT_QUERY))
    parser.add_argument(
        "--min-price",
        type=int,
        default=int(os.getenv("MIN_PRICE_YEN", str(DEFAULT_MIN_PRICE_YEN))),
        help="Minimum auction price in yen that should trigger a notification.",
    )
    parser.add_argument(
        "--max-price",
        type=int,
        default=int(os.getenv("MAX_PRICE_YEN", str(DEFAULT_MAX_PRICE_YEN))),
        help="Maximum auction price in yen that should trigger a notification.",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=int(os.getenv("MIN_SCORE", str(DEFAULT_MIN_SCORE))),
        help="Minimum keyword score required to include an item.",
    )
    parser.add_argument(
        "--exclude-keywords",
        default=os.getenv("EXCLUDE_KEYWORDS", ",".join(DEFAULT_EXCLUDED_KEYWORDS)),
        help="Comma-separated title keywords to exclude from notifications.",
    )
    parser.add_argument(
        "--priority-keywords",
        default=os.getenv("PRIORITY_KEYWORDS", ",".join(DEFAULT_PRIORITY_KEYWORDS)),
        help="Comma-separated title keywords to show first in notifications.",
    )
    parser.add_argument(
        "--state-file",
        default=os.getenv("STATE_FILE", DEFAULT_STATE_FILE),
        help="Path to JSON file used to avoid duplicate notifications.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the notification instead of sending it.")
    parser.add_argument(
        "--send-seen",
        action="store_true",
        help="Also notify auction IDs that were already sent before.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one check and exit instead of continuously watching.",
    )
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=float(os.getenv("CHECK_INTERVAL_MINUTES", str(DEFAULT_INTERVAL_MINUTES))),
        help="Continuous watch interval in minutes.",
    )
    return parser.parse_args()


def run_once(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file)
    excluded_keywords = tuple(keyword.strip() for keyword in args.exclude_keywords.split(",") if keyword.strip())
    priority_keywords = tuple(keyword.strip() for keyword in args.priority_keywords.split(",") if keyword.strip())

    html = fetch_search_results(args.query)
    matching_items = parse_auction_items(
        html,
        args.min_price,
        args.max_price,
        excluded_keywords,
        priority_keywords,
        args.min_score,
    )

    notified_ids = load_notified_ids(state_file)
    new_items = matching_items if args.send_seen else [item for item in matching_items if item.auction_id not in notified_ids]

price_map = load_price_map(state_file)
new_price_map = dict(price_map)

    
    if not new_items:
        print(f"No new Yahoo Auctions listings for {args.query!r} from {yen(args.min_price)} to {yen(args.max_price)}.")
        return 0

price_map = load_price_map(state_file)
new_price_map = dict(price_map)

for item in matching_items:
    item_id = item.auction_id
    current_price = item.price_yen

    if item_id in price_map:
        old_price = price_map[item_id]

        if current_price < old_price:
            msg = f"値下げ🔥 {old_price} → {current_price}\n{item.url}"
            send_discord_message(msg)

    new_price_map[item_id] = current_price

    message = build_message(args.query, args.min_price, args.max_price, args.min_score, new_items)

    if args.dry_run:
        print(message)
    else:
        send_discord_message(message)
        notified_ids.update(item.auction_id for item in new_items)
        save_notified_ids(state_file, notified_ids)
        print(f"Sent Discord notification for {len(new_items)} new listing(s).")

save_price_map(state_file, new_price_map)
    
    return 0


def main() -> int:
    args = parse_args()
    if args.once:
        return run_once(args)

    interval_seconds = max(60, int(args.interval_minutes * 60))
    print(f"Watching Yahoo Auctions every {interval_seconds // 60} minute(s). Press Ctrl+C to stop.")
    while True:
        try:
            run_once(args)
        except (requests.RequestException, RuntimeError) as exc:
            print(f"Check failed: {exc}", file=sys.stderr)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped Yahoo Auctions watcher.")
        raise SystemExit(0)
    except requests.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except requests.RequestException as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1)
