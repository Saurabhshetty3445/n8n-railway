"""
LeetCode Interview Experience Scraper
Hosted on Railway | Triggered by Make.com every 10 hours
Two endpoints: /list (metadata) and /scrape-content (full text)
Playwright replaces Selenium — faster startup, faster page loads.
"""

import os
import json
import time
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

LEETCODE_URL_1     = "https://leetcode.com/discuss/topic/interview-experience/"
LEETCODE_URL_2     = "https://leetcode.com/discuss/topic/interview/"
MAX_POSTS_PER_URL  = 6
MAX_POSTS_COMBINED = 12
PROCESSED_FILE     = "/data/processed_posts.json"
MAX_POSTS          = 6
REPROCESS_HOURS    = 10
SCRAPE_DELAY       = 0.3

app = Flask(__name__)


# ── Persistent State ──────────────────────────────────────────────────────────

def load_processed() -> dict:
    os.makedirs(os.path.dirname(PROCESSED_FILE), exist_ok=True)
    if not os.path.exists(PROCESSED_FILE):
        return {}
    try:
        with open(PROCESSED_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_processed(data: dict) -> None:
    os.makedirs(os.path.dirname(PROCESSED_FILE), exist_ok=True)
    with open(PROCESSED_FILE, "w") as f:
        json.dump(data, f, indent=2)


def post_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def already_processed(url: str, processed: dict) -> bool:
    h = post_hash(url)
    if h not in processed:
        return False
    scraped_at = processed[h].get("scraped_at", "")
    if not scraped_at:
        return True
    dt = datetime.fromisoformat(scraped_at)
    age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return age_hours < REPROCESS_HOURS


def mark_processed(url: str, processed: dict) -> None:
    processed[post_hash(url)] = {
        "url": url,
        "scraped_at": datetime.now(timezone.utc).isoformat()
    }


# ── Cookie handling ───────────────────────────────────────────────────────────

def load_cookies_from_env() -> Optional[list]:
    raw = os.environ.get("LEETCODE_COOKIES", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        log.error(f"Failed to parse LEETCODE_COOKIES: {e}")
        return None


def selenium_to_pw_cookie(ck: dict) -> dict:
    """Convert Selenium/JSON cookie dict to Playwright format."""
    pw = {
        "name":   ck.get("name", ""),
        "value":  ck.get("value", ""),
        "domain": ck.get("domain", ".leetcode.com"),
        "path":   ck.get("path", "/"),
    }
    if "secure" in ck:
        pw["secure"] = bool(ck["secure"])
    if "httpOnly" in ck:
        pw["httpOnly"] = bool(ck["httpOnly"])
    if "expirationDate" in ck:
        pw["expires"] = int(ck["expirationDate"])
    return pw


# ── Playwright Browser ────────────────────────────────────────────────────────

def build_browser(playwright):
    """Launch fast headless Chromium with anti-bot settings."""
    return playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--no-first-run",
            "--disable-default-apps",
            "--disable-background-networking",
            "--blink-settings=imagesEnabled=false",
        ]
    )


def build_context(browser, cookies: Optional[list] = None):
    """Create browser context with stealth + cookies."""
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 720},
        java_script_enabled=True,
        bypass_csp=True,
    )
    context.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    if cookies:
        pw_cookies = [selenium_to_pw_cookie(c) for c in cookies]
        try:
            context.add_cookies(pw_cookies)
            log.info(f"Injected {len(pw_cookies)} cookies")
        except Exception as e:
            log.warning(f"Cookie inject error: {e}")
    return context


# ── Scraping Logic ────────────────────────────────────────────────────────────

def scrape_post_detail(page, url: str) -> Optional[str]:
    """
    Scrape post content from LeetCode discuss post.
    Collects text from: p, ul, li, b, h1, h2, h3, h4, i, span tags
    inside div.break-words. Limit 6000 chars for AI safety.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)

        loaded = False
        for sel in ["div.break-words", "h1", "body"]:
            try:
                page.wait_for_selector(sel, timeout=8000)
                log.info(f"Post page loaded: {sel}")
                loaded = True
                break
            except PWTimeout:
                continue

        if not loaded:
            log.warning("Page load timeout — using whatever loaded")

        time.sleep(0.5)
        soup = BeautifulSoup(page.content(), "html.parser")

        for tag in soup.select("nav, footer, header, script, style, aside"):
            tag.decompose()

        CONTENT_TAGS = ["p", "ul", "li", "b", "h1", "h2", "h3", "h4", "i", "span"]
        lines = []

        def extract_from_container(container):
            for tag in container.find_all(CONTENT_TAGS):
                text = tag.get_text(separator=" ", strip=True)
                if text and len(text) > 1:
                    if tag.name in ["h1", "h2", "h3", "h4"]:
                        lines.append(f"[{tag.name.upper()}] {text}")
                    elif tag.name == "li":
                        lines.append(f"- {text}")
                    else:
                        lines.append(text)

        container = soup.select_one("div.break-words")
        if container:
            log.info("Primary container div.break-words found")
            extract_from_container(container)

        if not lines:
            log.warning("Primary empty — trying break-words class fallback")
            container = soup.find("div", class_=lambda c: c and "break-words" in c)
            if container:
                extract_from_container(container)

        if not lines:
            log.warning("Trying full page content tags")
            extract_from_container(soup)

        if not lines:
            log.warning("Using body text fallback")
            body_text = page.inner_text("body")
            lines = [body_text[:3000]]

        full_text = "\n".join(lines)
        full_text = re.sub(r"\n{3,}", "\n\n", full_text).strip()

        if len(full_text) > 6000:
            full_text = full_text[:6000].strip() + "..."
            log.info("Truncated to 6000 chars")
        else:
            log.info(f"Full content: {len(full_text)} chars")

        return full_text if full_text else None

    except Exception as e:
        log.error(f"Detail scrape failed for {url}: {e}")
        try:
            body_text = page.inner_text("body")
            return body_text[:6000].strip() if body_text else None
        except Exception:
            pass
        return None


def is_today_strict(timestamp: str) -> bool:
    t = timestamp.strip().lower()
    if not t:
        return False
    if re.search(r"[a-z]{3}\s+\d{1,2},?\s+\d{4}", t):
        return False
    if "yesterday" in t:
        return False
    if "week" in t or "month" in t or "year" in t:
        return False
    if re.search(r"(\d+)\s+day", t):
        return False
    if "just now" in t or "a few seconds" in t:
        return True
    if re.match(r"^a\s+minute", t):
        return True
    if re.match(r"^a\s+second", t):
        return True
    if re.match(r"^an?\s+hour", t):
        return True
    if re.search(r"(\d+)\s+second", t):
        return True
    min_m = re.search(r"(\d+)\s+minute", t)
    if min_m:
        return 1 <= int(min_m.group(1)) <= 59
    hr_m = re.search(r"(\d+)\s+hour", t)
    if hr_m:
        return 1 <= int(hr_m.group(1)) <= 23
    return False


def timestamp_to_sort_key(timestamp: str) -> int:
    from datetime import timedelta
    t = timestamp.strip().lower()
    now = datetime.now(timezone.utc)
    if not t:
        return 0
    m = re.search(r"(\d+)\s+minute", t)
    if m:
        return int((now - timedelta(minutes=int(m.group(1)))).timestamp())
    m = re.search(r"(\d+)\s+hour", t)
    if m:
        return int((now - timedelta(hours=int(m.group(1)))).timestamp())
    m = re.search(r"(\d+)\s+day", t)
    if m:
        return int((now - timedelta(days=int(m.group(1)))).timestamp())
    if "just now" in t or "second" in t:
        return int(now.timestamp())
    if "yesterday" in t:
        return int((now - timedelta(days=1)).timestamp())
    m = re.search(r"([a-z]{3})\s+(\d{1,2}),?\s+(\d{4})", t)
    if m:
        try:
            from datetime import datetime as dt2
            d = dt2.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y")
            return int(d.timestamp())
        except Exception:
            pass
    return 0


def scrape_listing(page, url: str, max_posts: int = 6) -> list:
    page.goto(url, wait_until="domcontentloaded", timeout=20000)

    waited = False
    for wait_sel in [
        "div.flex.flex-col.gap-4",
        "div[class*='topic-item']",
        "a[href*='/discuss/']",
        "div.overflow-hidden",
    ]:
        try:
            page.wait_for_selector(wait_sel, timeout=6000)
            log.info(f"Page loaded — wait selector matched: {wait_sel}")
            waited = True
            break
        except PWTimeout:
            continue

    if not waited:
        log.error("Timed out — no post cards found")
        log.info("PAGE TITLE: " + page.title())
        return []

    for _ in range(3):
        page.evaluate("window.scrollBy(0, 400)")
        time.sleep(0.3)

    soup = BeautifulSoup(page.content(), "html.parser")

    containers = soup.select("a[href*='/discuss/'][class*='no-underline']")

    if not containers:
        log.warning("Selector 1 empty, trying selector 2")
        containers = [
            a for a in soup.find_all("a", href=True)
            if re.search(r"/discuss/\d+/", a.get("href", ""))
        ]

    if not containers:
        log.warning("Selector 2 empty, trying selector 3")
        containers = [
            a for a in soup.find_all("a", href=True)
            if "/discuss/" in a.get("href", "") and len(a.get_text(strip=True)) > 10
        ]

    log.info(f"Raw containers found: {len(containers)}")

    posts = []
    seen_urls = set()

    for el in containers[: max_posts * 5]:
        if len(posts) >= max_posts:
            break

        href = el.get("href", "")
        post_url = f"https://leetcode.com{href}" if href.startswith("/") else href

        if not post_url or post_url in seen_urls:
            continue
        if "/discuss/topic/" in post_url or post_url in (LEETCODE_URL_1, LEETCODE_URL_2):
            continue
        seen_urls.add(post_url)

        title = ""
        for title_sel in [
            "div.text-sd-foreground.line-clamp-1",
            "div[class*='line-clamp-1']",
            "p[class*='line-clamp-1']",
            "span[class*='line-clamp-1']",
        ]:
            t = el.select_one(title_sel)
            if t:
                title = t.get_text(strip=True)
                break

        if not title:
            candidates = [
                tag.get_text(strip=True)
                for tag in el.find_all(["div", "p", "span", "h3"])
                if len(tag.get_text(strip=True)) > 10
            ]
            title = max(candidates, key=len) if candidates else el.get_text(strip=True)[:120]

        if not title:
            continue

        log.info(f"Post found: {title!r}")

        if not any(kw in title.lower() for kw in [
            "interview", "experience", "sde", "questions", "question",
            "swe", "rejected", "accepted", "reject", "accept", "oa"
        ]):
            log.info(f"Skipping — no keyword match: {title!r}")
            continue

        description = ""
        for desc_sel in [
            "div.text-sd-muted-foreground.line-clamp-2",
            "div[class*='line-clamp-2']",
            "p[class*='line-clamp-2']",
        ]:
            d = el.select_one(desc_sel)
            if d:
                description = d.get_text(strip=True)
                break

        timestamp = ""
        for ts_sel in [
            "span[data-state='closed']",
            "span[class*='text-sd-muted']",
            "span[class*='time']",
            "time",
        ]:
            t = el.select_one(ts_sel)
            if t:
                timestamp = t.get("datetime", "") or t.get_text(strip=True)
                break

        if not timestamp:
            full_el_text = el.get_text(" ", strip=True)
            m = re.search(r"(\d+\s+(?:minute|hour|day|week|month)s?\s+ago|just now|yesterday)", full_el_text, re.I)
            if m:
                timestamp = m.group(1)

        log.info(f"Timestamp: {timestamp!r}")

        if not is_today_strict(timestamp):
            log.info(f"Skipping — not today ({timestamp!r}): {title!r}")
            continue

        posts.append({
            "url":         post_url,
            "title":       title,
            "description": description,
            "timestamp":   timestamp,
            "sort_key":    timestamp_to_sort_key(timestamp),
        })
        time.sleep(SCRAPE_DELAY)

    posts.sort(key=lambda p: p["sort_key"], reverse=True)
    for p in posts:
        p.pop("sort_key", None)

    log.info(f"Returning {len(posts)} TODAY's interview posts (newest first)")
    for p in posts:
        log.info(f"  [{p['timestamp']}] {p['title']!r}")
    return posts


# ── /list endpoint ────────────────────────────────────────────────────────────

def run_list_cycle() -> dict:
    cookies = load_cookies_from_env()
    posts   = []

    try:
        with sync_playwright() as pw:
            browser = build_browser(pw)
            context = build_context(browser, cookies)
            page    = context.new_page()

            if cookies:
                page.goto("https://leetcode.com", wait_until="domcontentloaded", timeout=15000)
                time.sleep(0.5)

            log.info(f"Scraping URL1: {LEETCODE_URL_1}")
            raw1 = scrape_listing(page, LEETCODE_URL_1, max_posts=6)
            log.info(f"URL1 returned {len(raw1)} posts")

            log.info(f"Scraping URL2: {LEETCODE_URL_2}")
            raw2 = scrape_listing(page, LEETCODE_URL_2, max_posts=8)
            log.info(f"URL2 returned {len(raw2)} posts")

            page.close()
            browser.close()

        seen_urls = set()
        combined  = []
        for post in raw1 + raw2:
            if post["url"] not in seen_urls:
                seen_urls.add(post["url"])
                combined.append(post)

        combined.sort(key=lambda p: timestamp_to_sort_key(p.get("timestamp", "")), reverse=True)
        combined = combined[:MAX_POSTS_COMBINED]

        for post in combined:
            posts.append({
                "post_id":   post_hash(post["url"]),
                "title":     post["title"],
                "timestamp": post["timestamp"],
                "post_url":  post["url"],
            })

        log.info(f"List cycle done — {len(posts)} combined posts")

    except Exception as e:
        log.exception(f"List cycle crashed: {e}")
        return {"status": "error", "message": str(e), "posts": []}

    return {"status": "success", "count": len(posts), "posts": posts}


# ── /scrape-content endpoint ──────────────────────────────────────────────────

def run_content_scrape(post_url: str) -> dict:
    cookies = load_cookies_from_env()

    try:
        with sync_playwright() as pw:
            browser = build_browser(pw)
            context = build_context(browser, cookies)
            page    = context.new_page()

            if cookies:
                page.goto("https://leetcode.com", wait_until="domcontentloaded", timeout=15000)
                time.sleep(0.3)

            post_text = scrape_post_detail(page, post_url)

            page.close()
            browser.close()

        if post_text is None:
            return {"status": "error", "message": "Could not scrape post content", "content": ""}

        log.info(f"Content scraped ({len(post_text)} chars): {post_url}")
        return {"status": "success", "post_url": post_url, "content": post_text}

    except Exception as e:
        log.exception(f"Content scrape crashed: {e}")
        return {"status": "error", "message": str(e), "content": ""}


# ── Flask Endpoints ───────────────────────────────────────────────────────────

def auth_check() -> bool:
    api_key  = request.headers.get("X-API-Key", "")
    expected = os.environ.get("SCRAPER_API_KEY", "")
    return not expected or api_key == expected


@app.route("/list", methods=["GET", "POST"])
def list_endpoint():
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401
    result = run_list_cycle()
    return jsonify(result), 200 if result["status"] == "success" else 500


@app.route("/scrape-content", methods=["POST"])
def content_endpoint():
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401
    body     = request.get_json(force=True, silent=True) or {}
    post_url = body.get("post_url", "").strip()
    if not post_url:
        return jsonify({"error": "Missing post_url in request body"}), 400
    result = run_content_scrape(post_url)
    return jsonify(result), 200 if result["status"] == "success" else 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


@app.route("/processed", methods=["GET"])
def list_processed():
    return jsonify(load_processed())


@app.route("/clear", methods=["POST"])
def clear_processed():
    save_processed({})
    return jsonify({"status": "cleared"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
