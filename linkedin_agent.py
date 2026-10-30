#!/usr/bin/env python3
"""
LinkedIn Autonomous Recruiter Post Harvester, 1-Click Easy Apply & Connection Outreach Agent
Integrates with agent.py, config.json, Dropzone, and persistent Chrome browser session.

Features:
  1. Recruiter Post Harvester:
     - Scans LinkedIn Content/Posts in the last 24 hours for hiring announcements.
     - Extracts direct recruiter/founder emails and job context.
     - Drops tickets into Dropzone for instant tailored PDF cold-email dispatch.
  2. 1-Click Easy Apply Engine:
     - Searches LinkedIn Jobs with Easy Apply & Past 24h filters.
     - Solves multi-step Easy Apply dialogs using config.json qa_bank.
     - Strict rate-limiting (max 20/day) with human-like delays.
  3. Hiring Manager & Agency Recruiter Connection Outreach:
     - Connects with verified 24h recruiter post authors & tech staffing recruiters.
     - Enforces official LinkedIn limits (80 safe / 100 max weekly rolling limit, 12/day).
     - 7-Day platform restriction safety lock, 30,000 hard network cap, 30% acceptance health check.
"""

import os
import sys
import re
import json
import time
import random
import logging
import argparse
import subprocess
import urllib.parse
import fcntl
import io
from datetime import datetime, timedelta

from PIL import Image
import pytesseract
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import agent as email_agent
import naukri_agent
import optimizer

if os.path.exists('/opt/homebrew/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = '/opt/homebrew/bin/tesseract'

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')


def human_delay(min_sec=1.5, max_sec=3.5):
    """Sleep for a randomized human-like interval."""
    time.sleep(random.uniform(min_sec, max_sec))


import power_utils
from power_utils import MacKeepAwake


def notify_macos(title, message, is_urgent=False):
    """Display a non-blocking macOS notification banner respecting quiet_mode."""
    power_utils.notify_macos(title, message, is_urgent=is_urgent)


def load_linkedin_ledger(logs_dir):
    """Load persistent ledger of applied LinkedIn jobs and harvested recruiter posts."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = os.path.join(logs_dir, 'linkedin_applied_jobs.json')
    default_data = {
        "last_daily_auto_run_date": None,
        "login_verified": False,
        "applied_jobs": {},
        "harvested_posts": {}
    }
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    default_data.update(data)
                    if not isinstance(default_data.get("applied_jobs"), dict):
                        default_data["applied_jobs"] = {}
                    if not isinstance(default_data.get("harvested_posts"), dict):
                        default_data["harvested_posts"] = {}
        except Exception as e:
            logging.warning(f"[LinkedIn] Could not read linkedin_applied_jobs.json: {e}")
    return default_data


def save_linkedin_ledger(logs_dir, ledger):
    """Persist LinkedIn ledger state."""
    ledger_path = os.path.join(logs_dir, 'linkedin_applied_jobs.json')
    try:
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
    except Exception as e:
        logging.warning(f"[LinkedIn] Could not save linkedin_applied_jobs.json: {e}")


def count_linkedin_applies_today(ledger):
    """Count how many LinkedIn jobs were applied to today."""
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    applied = ledger.get("applied_jobs", {})
    count = 0
    for _, info in applied.items():
        if not isinstance(info, dict):
            continue
        status = info.get("status", "")
        ts = str(info.get("applied_at", ""))
        if status == "APPLIED" and ts.startswith(today_prefix):
            count += 1
    return count


def count_linkedin_harvested_today(ledger):
    """Count how many LinkedIn recruiter posts were harvested today."""
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    harvested = ledger.get("harvested_posts", {})
    count = 0
    for _, info in harvested.items():
        if isinstance(info, dict) and str(info.get("harvested_at", "")).startswith(today_prefix):
            count += 1
    return count


def load_linkedin_connections_ledger(logs_dir):
    """Load persistent ledger of LinkedIn connection outreach requests."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = os.path.join(logs_dir, 'linkedin_connections.json')
    default_data = {
        "account_tier": "standard",
        "hard_network_cap": 30000,
        "weekly_stats": {
            "current_week_start": None,
            "invites_sent_this_week": 0,
            "weekly_cap": 80,
            "platform_max_limit": 100
        },
        "daily_stats": {
            "today_date": None,
            "invites_sent_today": 0,
            "daily_cap": 12
        },
        "platform_restriction": {
            "is_restricted": False,
            "restricted_at": None,
            "restricted_until": None,
            "reason": None
        },
        "connections": {}
    }
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    default_data.update(data)
                    if not isinstance(default_data.get("connections"), dict):
                        default_data["connections"] = {}
                    if not isinstance(default_data.get("weekly_stats"), dict):
                        default_data["weekly_stats"] = {}
                    if not isinstance(default_data.get("daily_stats"), dict):
                        default_data["daily_stats"] = {}
                    if not isinstance(default_data.get("platform_restriction"), dict):
                        default_data["platform_restriction"] = {
                            "is_restricted": False,
                            "restricted_at": None,
                            "restricted_until": None,
                            "reason": None
                        }
        except Exception as e:
            logging.warning(f"[LinkedIn Outreach] Could not read linkedin_connections.json: {e}")
    return default_data


def is_platform_restriction_active(conn_ledger):
    """Check if LinkedIn 7-day platform invitation block is active."""
    res = conn_ledger.get("platform_restriction", {})
    if not res.get("is_restricted"):
        return False, None
    res_until = res.get("restricted_until")
    if not res_until:
        return False, None
    try:
        until_dt = datetime.strptime(res_until[:19], "%Y-%m-%d %H:%M:%S")
        if datetime.now() < until_dt:
            return True, res_until
        else:
            # Cooldown period expired, auto-clear restriction
            res["is_restricted"] = False
            return False, None
    except Exception:
        return False, None


def check_and_log_acceptance_health(conn_ledger, min_rate=0.30):
    """
    Monitor invitation acceptance health to avoid LinkedIn algorithm penalties.
    LinkedIn flags accounts with acceptance rate < 30% or high 'I don't know this person' flags.
    """
    conns = conn_ledger.get("connections", {})
    total_invited = 0
    total_accepted = 0
    stale_pending = 0
    now = datetime.now()
    two_weeks_ago = now - timedelta(days=14)

    for _, info in conns.items():
        if not isinstance(info, dict):
            continue
        status = info.get("status", "")
        if status in ("INVITED", "SENT", "ACCEPTED"):
            total_invited += 1
            if status == "ACCEPTED":
                total_accepted += 1
            elif status in ("INVITED", "SENT"):
                ts = str(info.get("sent_at", ""))
                try:
                    dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                    if dt < two_weeks_ago:
                        stale_pending += 1
                except Exception:
                    pass

    if total_invited >= 15:
        rate = total_accepted / total_invited
        if rate < min_rate:
            logging.warning(
                f"[LinkedIn Outreach] ⚠️ Acceptance Rate Notice: Current rate is {rate:.1%} "
                f"(recommended >= {min_rate:.0%}). LinkedIn algorithm lowers weekly limits if acceptance drops too low. "
                "Focusing exclusively on active hiring post authors and verified tech recruiters."
            )
    if stale_pending > 50:
        logging.info(
            f"[LinkedIn Outreach] 💡 Clean-Up Notice: {stale_pending} pending invitations are older than 14 days. "
            "Periodically withdrawing very old pending invites keeps your account SSI score high."
        )


def save_linkedin_connections_ledger(logs_dir, ledger):
    """Persist LinkedIn connection ledger state."""
    ledger_path = os.path.join(logs_dir, 'linkedin_connections.json')
    try:
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
    except Exception as e:
        logging.warning(f"[LinkedIn Outreach] Could not save linkedin_connections.json: {e}")


def count_connections_today(ledger):
    """Count how many connection requests were sent today."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    count = 0
    for _, info in ledger.get("connections", {}).items():
        if not isinstance(info, dict):
            continue
        status = info.get("status", "")
        ts = str(info.get("sent_at", ""))
        if status in ("INVITED", "SENT") and ts.startswith(today_str):
            count += 1
    return count


def count_connections_last_7_days(ledger):
    """Count how many connection requests were sent in the last rolling 7 days."""
    now = datetime.now()
    cutoff = now - timedelta(days=7)
    count = 0
    for _, info in ledger.get("connections", {}).items():
        if not isinstance(info, dict):
            continue
        status = info.get("status", "")
        ts_str = str(info.get("sent_at", ""))
        if status in ("INVITED", "SENT") and ts_str:
            try:
                dt = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
                if dt >= cutoff:
                    count += 1
            except Exception:
                pass
    return count


def launch_linkedin_browser(playwright, config, force_headed=False):
    """Launch persistent Google Chrome context dedicated to LinkedIn automation."""
    li_cfg = config.get('linkedin', {})
    profile_dir = os.path.expanduser(
        li_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/linkedin')
    )
    os.makedirs(profile_dir, exist_ok=True)
    naukri_agent.cleanup_stale_chrome_locks(profile_dir)

    chrome_exec = li_cfg.get('chrome_executable', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    headless = False if force_headed else bool(li_cfg.get('headless', False))

    launch_kwargs = {
        "user_data_dir": profile_dir,
        "headless": headless,
        "viewport": {"width": 1440, "height": 900},
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    }
    if os.environ.get("JOB_AUTO_OFFSCREEN") == "1" or li_cfg.get("offscreen", False):
        launch_kwargs["args"].append("--window-position=-3000,-3000")

    if os.path.exists(chrome_exec):
        launch_kwargs["executable_path"] = chrome_exec

    context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)
    return context


def is_logged_in_to_linkedin(page):
    """Check whether current LinkedIn page represents an authenticated user session."""
    url = (page.url or "").lower()
    if "login" in url or "signup" in url or "authwall" in url:
        return False

    logged_in_selectors = [
        "nav.global-nav",
        ".global-nav__me",
        "input[role='combobox'][aria-label*='Search']",
        ".share-box-feed-entry__trigger",
        "div[data-control-name='nav.settings']",
        "header.global-nav",
        "#global-nav-typeahead"
    ]
    for sel in logged_in_selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=800):
                return True
        except Exception:
            pass

    return "feed" in url or "jobs" in url or "mynetwork" in url


def ensure_logged_in(page, interactive_wait_seconds=300):
    """Verify LinkedIn login session or prompt user to log in once."""
    logging.info("[LinkedIn] Navigating to LinkedIn feed to verify session...")
    try:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        logging.warning(f"[LinkedIn] Initial navigation warning: {e}")

    human_delay(2.5, 4.0)

    if is_logged_in_to_linkedin(page):
        logging.info("[LinkedIn] Active LinkedIn session confirmed!")
        return True

    msg = "Please log in to your LinkedIn account in the opened Chrome window. Session will be saved automatically."
    logging.warning(f"[LinkedIn] Not logged in yet. {msg}")
    print(f"\n[ACTION REQUIRED] {msg}\nWaiting up to {interactive_wait_seconds} seconds for login...")
    notify_macos("LinkedIn Automation", "Please log in once in the opened Chrome window.")

    try:
        if "linkedin.com" not in (page.url or "") or "login" not in (page.url or ""):
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    start_time = time.time()
    while time.time() - start_time < interactive_wait_seconds:
        time.sleep(3)
        try:
            if is_logged_in_to_linkedin(page):
                logging.info("[LinkedIn] Login detected! Saving persistent session.")
                print("[SUCCESS] LinkedIn login detected and saved to persistent Chrome profile!")
                human_delay(2.0, 3.5)
                return True
        except Exception:
            pass

    logging.error("[LinkedIn] Timed out waiting for LinkedIn login.")
    return False


# -------------------------------------------------------------------------
# ENGINE 1: 24-HOUR RECRUITER POST HARVESTER & EMAIL BRIDGE
# -------------------------------------------------------------------------

EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b')
EXCLUDED_EMAIL_DOMAINS = {
    'example.com', 'email.com', 'yourcompany.com', 'domain.com', 'company.com',
    'linkedin.com', 'gmail.com.br', 'google.com', 'apple.com', 'microsoft.com'
}


def deobfuscate_email_text(text):
    """Normalize obfuscated emails such as user [at] domain [dot] com or user AT company DOT in."""
    if not text:
        return ""
    # 1. Bracketed/parenthesized at and dot: [at], (at), {at}, [dot], (dot), {dot}
    s = re.sub(r'[\(\[\{]\s*(?:at|@)\s*[\)\]\}]', '@', text, flags=re.I)
    s = re.sub(r'[\(\[\{]\s*(?:dot|\.)\s*[\)\]\}]', '.', s, flags=re.I)

    # 2. 'name AT domain DOT ext'
    s = re.sub(r'\b([A-Za-z0-9._%+-]+)\s+(?:at|AT)\s+([A-Za-z0-9-]+)\s+(?:dot|DOT)\s+([A-Za-z]{2,7})\b', r'\1@\2.\3', s)

    # 3. 'name at domain.ext' (e.g. 'amrit at gmail.com')
    s = re.sub(r'\b([A-Za-z0-9._%+-]+)\s+(?:at|AT)\s+([A-Za-z0-9-]+\.[A-Za-z]{2,7})\b', r'\1@\2', s, flags=re.I)

    # 4. Spaced out @ and .: 'name @ domain . com'
    s = re.sub(r'([A-Za-z0-9._%+-])\s*@\s*([A-Za-z0-9.-]+)', r'\1@\2', s)
    s = re.sub(r'@([A-Za-z0-9.-]+?)\s*\.\s*([A-Za-z]{2,7})\b', r'@\1.\2', s)
    return s


def extract_valid_emails_from_text(text):
    """Extract clean recruiter email addresses from post content with de-obfuscation and typo healing."""
    if not text:
        return []
    deobfuscated = deobfuscate_email_text(text)
    candidates = EMAIL_REGEX.findall(deobfuscated)
    valid = []
    for em in candidates:
        healed = email_agent.heal_ocr_email_typos(em)
        em_lower = healed.lower().strip('.').strip(',').strip(';').strip(':')
        # Skip image formats or junk match
        if any(em_lower.endswith(ext) for ext in ('.png', '.jpg', '.jpeg', '.webp', '.svg')):
            continue
        domain = em_lower.split('@')[-1]
        if domain in EXCLUDED_EMAIL_DOMAINS or len(domain) < 4:
            continue
        if em_lower not in valid:
            valid.append(em_lower)
    return valid


def ocr_image_element(img_loc):
    """Capture screenshot of an in-page image element and extract text using Tesseract OCR."""
    try:
        if not img_loc or img_loc.count() == 0:
            return ""
        if not img_loc.is_visible(timeout=500):
            return ""
        img_bytes = img_loc.screenshot(timeout=2500)
        if not img_bytes or len(img_bytes) < 500:
            return ""
        image = Image.open(io.BytesIO(img_bytes))
        ocr_text = pytesseract.image_to_string(image)
        return ocr_text.strip() if ocr_text else ""
    except Exception as e:
        logging.debug(f"[LinkedIn Harvester] Image OCR skipped/failed: {e}")
        return ""


def apply_route_optimizations(page, enabled=True):
    """Block video streams, heavy web fonts, and telemetry trackers to dramatically speed up scraping."""
    if not enabled:
        return
    try:
        blocked_pattern = re.compile(
            r'(\.(mp4|webm|m3u8|ts|woff|woff2|ttf|otf)(\?.*)?$|ads\.linkedin\.com|px\.ads\.linkedin\.com|snap\.licdn\.com)',
            re.IGNORECASE
        )
        page.route(blocked_pattern, lambda route: route.abort())
    except Exception as e:
        logging.debug(f"[LinkedIn] Route optimization warning: {e}")


def load_linkedin_query_stats(logs_dir):
    """Load query yield statistics to prioritize top-performing queries."""
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(logs_dir, 'linkedin_query_stats.json')
    default_stats = {
        "queries": {},
        "discovered_keywords": [],
        "last_updated": None
    }
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    default_stats.update(data)
                    if not isinstance(default_stats.get("queries"), dict):
                        default_stats["queries"] = {}
        except Exception as e:
            logging.debug(f"[LinkedIn] Could not read query stats: {e}")
    return default_stats


def save_linkedin_query_stats(logs_dir, stats):
    """Persist query yield statistics."""
    path = os.path.join(logs_dir, 'linkedin_query_stats.json')
    try:
        stats["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2, sort_keys=True)
    except Exception as e:
        logging.debug(f"[LinkedIn] Could not save query stats: {e}")


def rank_queries_by_yield(queries, query_stats):
    """Rank queries so historically high-yield queries run first, with optimistic exploration for newer queries."""
    q_map = query_stats.get("queries", {})

    def score_query(q):
        info = q_map.get(q)
        if not info:
            return 2.0  # High initial optimism for untested queries
        runs = int(info.get("runs", 0))
        harvested = int(info.get("leads_harvested", 0))
        if runs == 0:
            return 2.0
        yield_rate = harvested / runs
        ucb_bonus = (2.0 / (runs + 1)) ** 0.5
        return yield_rate + ucb_bonus

    return sorted(queries, key=score_query, reverse=True)


def harvest_recruiter_posts(page, config, ledger, logs_dir, max_harvest_override=None):
    """
    Search LinkedIn Posts from the past 24 hours:
      1. Uses multi-armed bandit ranking to prioritize high-yield query streams.
      2. Dynamically scrolls and discovers recruiter/founder hiring announcements.
      3. Uses regex + email de-obfuscation + OCR on flyer posters to extract direct contacts.
      4. Drops rich .txt tickets into Dropzone for instant tailored cold-emailing.
    """
    li_cfg = config.get('linkedin', {})
    search_cfg = li_cfg.get('search', {})
    daily_harvest_limit = int(li_cfg.get('daily_harvest_limit', 100))
    harvest_per_run = int(li_cfg.get('post_harvest_limit_per_run', 50))
    max_harvest = int(max_harvest_override) if max_harvest_override is not None else harvest_per_run
    max_scrolls = int(li_cfg.get('max_scrolls_per_query', 8))
    enable_ocr_flyers = bool(li_cfg.get('enable_ocr_flyers', True))
    route_blocking = bool(li_cfg.get('route_blocking_enabled', True))
    auto_prioritize = bool(li_cfg.get('auto_prioritize_queries', True))

    harvested_today = count_linkedin_harvested_today(ledger)
    logging.info(
        f"[LinkedIn Harvester] Status today: {harvested_today}/{daily_harvest_limit} harvested so far "
        f"(Target this run: {max_harvest})."
    )
    if max_harvest_override is None and harvested_today >= daily_harvest_limit:
        logging.info(f"[LinkedIn Harvester] Reached daily harvest limit ({daily_harvest_limit}). Skipping post harvest for today.")
        return 0

    remaining_daily = max(0, daily_harvest_limit - harvested_today)
    effective_run_limit = min(max_harvest, remaining_daily) if max_harvest_override is None else max_harvest

    apply_route_optimizations(page, enabled=route_blocking)

    raw_queries = search_cfg.get('post_search_queries', [
        "hiring (\"Java\" OR \"Spring Boot\") (\"email\" OR \"send resume\" OR \"share CV\" OR \"drop CV\")",
        "\"we are hiring\" (\"Java Full Stack\" OR \"Spring Boot\" OR \"Microservices\") (\"@gmail.com\" OR \"@\" OR \"share your profile\")",
        "\"immediate joiner\" (\"Java\" OR \"Spring Boot\") (\"notice period\" OR \"0-15 days\" OR \"immediate\")",
        "\"urgent requirement\" (\"Java Developer\" OR \"Full Stack\") (\"share profile\" OR \"drop cv\" OR \"send cv\")",
        "\"Java\" \"React\" (\"hiring\" OR \"job opening\") (\"careers@\" OR \"reach out\" OR \"send resume\")"
    ])

    query_stats = load_linkedin_query_stats(logs_dir)
    queries = rank_queries_by_yield(raw_queries, query_stats) if auto_prioritize else raw_queries

    dropzone_dir = email_agent.get_dropzone_path(config)
    os.makedirs(dropzone_dir, exist_ok=True)

    harvested_ledger = ledger.setdefault("harvested_posts", {})
    sent_emails_path = os.path.join(logs_dir, "sent_emails.json")
    already_sent_emails = set()
    if os.path.exists(sent_emails_path):
        try:
            with open(sent_emails_path, 'r', encoding='utf-8') as f:
                already_sent_emails = set(json.load(f).keys())
        except Exception:
            pass

    total_harvested_this_run = 0
    seen_activity_urns = set()

    hiring_triggers = (
        "hir", "open", "look", "join", "@", "mail", "cv", "resum", "requir",
        "candidat", "developer", "engineer", "stack", "java", "spring", "fastapi"
    )

    def _process_post_element(post_loc, source_label, q_stat_obj):
        nonlocal total_harvested_this_run
        if total_harvested_this_run >= effective_run_limit:
            return 0

        harvested_from_post = 0
        try:
            # Fast URN fingerprint check to prevent reprocessing posts in 0ms
            post_urn = ""
            urn_attr = post_loc.get_attribute("data-urn") or post_loc.get_attribute("data-chameleon-result-urn") or ""
            if "urn:li:activity:" in urn_attr:
                post_urn = urn_attr
            else:
                nested_urn = post_loc.locator("[data-urn*='urn:li:activity'], [data-chameleon-result-urn*='urn:li:activity']").first
                if nested_urn.count() > 0:
                    post_urn = nested_urn.get_attribute("data-urn") or nested_urn.get_attribute("data-chameleon-result-urn") or ""

            if not post_urn:
                link_loc = post_loc.locator("a[href*='urn:li:activity'], a[href*='/feed/update/']").first
                if link_loc.count() > 0:
                    href_val = link_loc.get_attribute("href") or ""
                    m = re.search(r'(urn:li:activity:\d+)', href_val)
                    if m:
                        post_urn = m.group(1)
                    elif "/feed/update/" in href_val:
                        post_urn = href_val.split('?')[0].rstrip('/')

            # Fast pre-check: inspect visible text before expanding "...see more"
            initial_snippet = ""
            try:
                initial_snippet = (post_loc.inner_text(timeout=1500) or "").strip()
            except Exception:
                initial_snippet = ""

            if not initial_snippet or len(initial_snippet) < 25:
                return 0

            # Fallback URN if none found on DOM attributes
            if not post_urn:
                post_urn = f"snippet_hash_{abs(hash(initial_snippet[:80]))}"

            if post_urn in seen_activity_urns:
                return 0
            seen_activity_urns.add(post_urn)

            if q_stat_obj is not None:
                q_stat_obj["posts_scanned"] = int(q_stat_obj.get("posts_scanned", 0)) + 1

            lower_snippet = initial_snippet.lower()
            has_trigger = any(t in lower_snippet for t in hiring_triggers)

            # Expand "...see more" if truncated and relevant
            see_more_btn = post_loc.locator(
                "button.feed-shared-inline-show-more-text__see-more-less-toggle, "
                "button:has-text('see more'), "
                "button:has-text('…see more'), "
                "button:has-text('more'), "
                "span:has-text('…see more'), "
                ".feed-shared-text-view button"
            ).first
            if see_more_btn.count() > 0 and see_more_btn.is_visible(timeout=500) and has_trigger:
                try:
                    see_more_btn.click(timeout=1200)
                    page.wait_for_timeout(250)
                except Exception:
                    pass

            try:
                post_text = (post_loc.inner_text(timeout=2000) or "").strip()
            except Exception:
                post_text = initial_snippet

            if not post_text or len(post_text) < 30:
                return 0

            # Extract author info with fallback to modern feed-actor layout
            author_name = "Recruiter"
            author_desc = ""
            author_url = ""

            lines = [l.strip() for l in post_text.split('\n') if l.strip()]
            start_idx = 1 if lines and lines[0] == 'Feed post' else 0
            if len(lines) > start_idx:
                candidate_name = lines[start_idx].split('•')[0].strip()
                if 2 <= len(candidate_name) <= 50 and candidate_name.lower() not in ('follow', 'connect'):
                    author_name = candidate_name

            for l in lines[start_idx+1:start_idx+6]:
                if not l.startswith('•') and not re.match(r'^\d+[smhdwy]\b', l) and l.lower() not in ('follow', 'connect', 'following', 'feed post'):
                    author_desc = l
                    break

            if author_name == "Recruiter":
                author_loc = post_loc.locator("[data-view-name='feed-actor'], .update-components-actor__name, .feed-shared-actor__name, strong").first
                if author_loc.count() > 0:
                    raw_name = (author_loc.inner_text(timeout=250) or "").split('•')[0].split('\n')[0].strip()
                    if raw_name:
                        author_name = raw_name

            if not author_desc:
                desc_loc = post_loc.locator(".update-components-actor__description, .feed-shared-actor__description").first
                if desc_loc.count() > 0:
                    author_desc = (desc_loc.inner_text(timeout=250) or "").strip()

            try:
                actor_link = post_loc.locator("a[data-view-name='feed-actor'], a.update-components-actor__image, a.feed-shared-actor__image, a.app-aware-link[href*='/in/'], a[href*='/company/']").first
                if actor_link.count() > 0:
                    raw_href = actor_link.get_attribute("href") or ""
                    author_url = raw_href.split('?')[0].strip()
            except Exception:
                pass

            post_url = f"https://www.linkedin.com/feed/update/{post_urn}/" if post_urn else ""

            # Extract emails (with de-obfuscation)
            emails = extract_valid_emails_from_text(post_text)

            # Top comment fallback if text mentions "comment"
            if not emails and any(hint in post_text.lower() for hint in ("in comment", "first comment", "1st comment", "comment section")):
                try:
                    comment_loc = post_loc.locator(".comments-comment-item__main-content, .feed-shared-comments-list, .comment-item").first
                    if comment_loc.count() > 0 and comment_loc.is_visible(timeout=300):
                        c_text = comment_loc.inner_text(timeout=400) or ""
                        c_emails = extract_valid_emails_from_text(c_text)
                        if c_emails:
                            emails.extend(c_emails)
                except Exception:
                    pass

            # Flyer Image OCR Fallback: if no email in text, check post image
            if not emails and enable_ocr_flyers:
                try:
                    img_loc = post_loc.locator(
                        "img.update-components-image__image, div.feed-shared-image img, "
                        "div[data-view-name*='feed-full-update'] img, .feed-shared-mini-update-v2__preview-image"
                    ).first
                    if img_loc.count() > 0 and img_loc.is_visible(timeout=300):
                        ocr_text = ocr_image_element(img_loc)
                        if ocr_text:
                            ocr_emails = extract_valid_emails_from_text(ocr_text)
                            if ocr_emails:
                                logging.info(f"[LinkedIn Harvester] 🖼️ Flyer OCR extracted email(s): {ocr_emails}")
                                emails.extend(ocr_emails)
                                post_text += f"\n\n[Attached Flyer OCR Text]:\n{ocr_text}"
                except Exception as e:
                    logging.debug(f"[LinkedIn Harvester] Flyer OCR check error: {e}")

            # Check if post contains an external ATS / Google Forms / MS Forms link (including lnkd.in shorteners)
            import ats_bridge
            ats_type, ats_url = ats_bridge.find_ats_url_in_text(post_text, resolve_shorteners=True)
            if ats_type and ats_url:
                logging.info(f"[LinkedIn Harvester] 🎯 Detected {ats_type.upper()} application link in post by {author_name}: {ats_url}")
                ats_bridge.queue_ats_job_to_dropzone(
                    ats_type, ats_url,
                    title=author_desc or "Software Engineer",
                    company=author_name,
                    source="LinkedIn_Post",
                    jd_text=post_text,
                    config=config
                )
                if not emails:
                    return 1

            if not emails:
                return 0

            for target_email in emails:
                if total_harvested_this_run >= effective_run_limit:
                    break
                if target_email in already_sent_emails:
                    logging.debug(f"[LinkedIn Harvester] Email {target_email} was already emailed previously. Skipping.")
                    continue
                if target_email in harvested_ledger:
                    logging.debug(f"[LinkedIn Harvester] Post with {target_email} already in ledger. Skipping.")
                    continue

                # Autonomous Lead Quality Evaluation
                lead_eval = optimizer.score_lead_relevance(
                    author_desc, content_text=post_text, recruiter_email=target_email
                )
                if lead_eval['action'] == 'DISCARD':
                    logging.debug(f"[LinkedIn Harvester] Skipped low-relevance post ({lead_eval['score']}/100) for {target_email}: {', '.join(lead_eval['reasons'])}")
                    continue

                # Create enriched Dropzone Ticket file
                clean_author = re.sub(r'[^a-zA-Z0-9]', '_', author_name)[:25]
                ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                ticket_filename = f"LinkedIn_Post_{clean_author}_{ts_str}.txt"
                ticket_path = os.path.join(dropzone_dir, ticket_filename)

                ticket_content = (
                    f"Source: LinkedIn Recruiter Post ({source_label})\n"
                    f"Recruiter / Author: {author_name}\n"
                    f"Designation: {author_desc}\n"
                    f"Target Email: {target_email}\n"
                    f"Post URL: {post_url or '(Direct Feed)'}\n"
                    f"Author Profile: {author_url or '(Not available)'}\n"
                    f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"--------------------------------------------------\n"
                    f"Job Description / Post Body:\n"
                    f"{post_text}\n"
                )

                with open(ticket_path, "w", encoding="utf-8") as tf:
                    tf.write(ticket_content)

                harvested_ledger[target_email] = {
                    "author": author_name,
                    "headline": author_desc,
                    "post_url": post_url,
                    "author_profile": author_url,
                    "harvested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ticket_file": ticket_filename,
                    "query": source_label
                }
                save_linkedin_ledger(logs_dir, ledger)

                # Update query performance statistics
                if q_stat_obj is not None:
                    q_stat_obj["leads_harvested"] = int(q_stat_obj.get("leads_harvested", 0)) + 1
                    if target_email not in q_stat_obj.get("emails", []):
                        q_stat_obj.setdefault("emails", []).append(target_email)
                    save_linkedin_query_stats(logs_dir, query_stats)

                harvested_from_post += 1
                total_harvested_this_run += 1
                success_msg = f"🎯 Harvested Recruiter Lead ({source_label}): {target_email} from '{author_name}' ({author_desc})"
                print(f"\n{success_msg}\n")
                logging.info(f"[LinkedIn Harvester] {success_msg}")
                notify_macos("LinkedIn Recruiter Lead", f"Found {target_email} ({author_name})")

        except Exception as e:
            logging.debug(f"[LinkedIn Harvester] Error reading post item: {e}")

        return harvested_from_post

    # -------------------------------------------------------------------------
    # STAGE 1: Relevance-Ranked Content Search (Past 24h & Prioritized queries)
    # -------------------------------------------------------------------------
    max_search_queries = min(len(queries), int(li_cfg.get('max_search_queries_per_run', 8)))
    search_queries_to_run = queries[:max_search_queries]
    logging.info(f"[LinkedIn Harvester] STAGE 1: Running top {len(search_queries_to_run)} prioritized query streams...")

    CONTENT_POST_SELECTORS = (
        "li.reusable-search__result-container, "
        "div[data-view-name*='search-entity-result'], "
        "div[data-view-name*='feed-full-update'], "
        "div.feed-shared-update-v2, "
        "div[data-urn*='urn:li:activity'], "
        "div[data-chameleon-result-urn], "
        "article[data-activity-urn], "
        "article, "
        "div.fie-impression-container, "
        ".search-results-container div.artdeco-card"
    )

    rate_limit_encountered = False
    for q_idx, query_str in enumerate(search_queries_to_run):
        if total_harvested_this_run >= effective_run_limit:
            logging.info(f"[LinkedIn Harvester] Reached harvest limit ({effective_run_limit}). Stopping.")
            break

        logging.info(f"[LinkedIn Harvester] Search Query {q_idx+1}/{len(search_queries_to_run)}: '{query_str}'")
        q_stat = query_stats.setdefault("queries", {}).setdefault(query_str, {
            "runs": 0,
            "posts_scanned": 0,
            "leads_harvested": 0,
            "emails": []
        })
        q_stat["runs"] = int(q_stat.get("runs", 0)) + 1

        encoded_q = urllib.parse.quote(query_str)
        # Use relevance ranking (natural LinkedIn sorting) with past-24h filter
        # Do NOT force sortBy="date_posted", as purely chronological sorting buries top recruiter announcements under raw noise.
        search_url = f"https://www.linkedin.com/search/results/content/?datePosted=%22past-24h%22&keywords={encoded_q}&origin=SWITCH_SEARCH_VERTICAL"
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            human_delay(2.5, 3.8)
            try:
                page.wait_for_selector(
                    "li.reusable-search__result-container, div.feed-shared-update-v2, div[data-view-name*='feed'], div.search-results-container",
                    timeout=5000
                )
            except Exception:
                pass
        except Exception as e:
            logging.warning(f"[LinkedIn Harvester] Navigation failed for query: {e}")
            continue

        # Check for LinkedIn Search Rate-Limit / Traffic pause
        body_text = (page.locator("body").inner_text(timeout=1000) or "").lower()
        if "unusual search traffic" in body_text or "search activity on your account has been paused" in body_text:
            warn_msg = "⚠️ [LinkedIn Harvester] Search paused by LinkedIn ('unusual search traffic detected'). Switching to Home Feed Stream."
            logging.warning(warn_msg)
            print(f"\n{warn_msg}\n")
            notify_macos("LinkedIn Rate Limit", "Search paused. Switching to Home Feed.")
            rate_limit_encountered = True
            break

        scroll_count = 0
        consecutive_no_new_posts = 0
        prev_post_count = 0

        while scroll_count < max_scrolls and total_harvested_this_run < effective_run_limit:
            post_locators = page.locator(CONTENT_POST_SELECTORS).all()
            if not post_locators:
                # Deep fallback for search container variants
                post_locators = page.locator("div.artdeco-card:has(.update-components-actor), div:has(span.feed-shared-text)").all()

            current_post_count = len(post_locators)
            logging.debug(f"[LinkedIn Harvester] Scroll {scroll_count+1}/{max_scrolls}: Found {current_post_count} post container(s).")

            for post_loc in post_locators:
                if total_harvested_this_run >= effective_run_limit:
                    break
                _process_post_element(post_loc, query_str, q_stat)

            if current_post_count == 0:
                if scroll_count < 3:
                    page.evaluate("window.scrollBy(0, 900)")
                    human_delay(2.0, 3.0)
                    scroll_count += 1
                    continue
                else:
                    logging.debug(f"[LinkedIn Harvester] No post containers found after {scroll_count} scrolls for '{query_str}'.")
                    break

            # Dynamic progressive scroll
            page.evaluate("window.scrollBy(0, 1000)")
            human_delay(1.5, 2.4)
            scroll_count += 1

            if current_post_count == prev_post_count:
                consecutive_no_new_posts += 1
                if consecutive_no_new_posts >= 3:
                    logging.debug(f"[LinkedIn Harvester] No new posts loaded after {scroll_count} scrolls. Ending query stream.")
                    break
            else:
                consecutive_no_new_posts = 0

            prev_post_count = current_post_count

    # -------------------------------------------------------------------------
    # STAGE 1B: Adaptive Freshness Fallback (Past Week)
    # If past-24h yielded 0 leads, broaden search to past week to capture active postings from the last 2-4 days
    # -------------------------------------------------------------------------
    if total_harvested_this_run == 0 and not rate_limit_encountered:
        logging.info("[LinkedIn Harvester] Past-24h yielded 0 leads. Broadening search to Past Week to capture active recent openings...")
        fallback_queries = search_queries_to_run[:3]
        for q_idx, query_str in enumerate(fallback_queries):
            if total_harvested_this_run >= effective_run_limit:
                break
            logging.info(f"[LinkedIn Harvester] Past-Week Fallback Query {q_idx+1}/{len(fallback_queries)}: '{query_str}'")
            encoded_q = urllib.parse.quote(query_str)
            search_url = f"https://www.linkedin.com/search/results/content/?datePosted=%22past-week%22&keywords={encoded_q}&origin=SWITCH_SEARCH_VERTICAL"
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                human_delay(2.5, 3.5)
            except Exception:
                continue

            scroll_count = 0
            while scroll_count < min(max_scrolls, 5) and total_harvested_this_run < effective_run_limit:
                post_locators = page.locator(CONTENT_POST_SELECTORS).all()
                if not post_locators:
                    post_locators = page.locator("div.artdeco-card:has(.update-components-actor), div:has(span.feed-shared-text)").all()
                for post_loc in post_locators:
                    if total_harvested_this_run >= effective_run_limit:
                        break
                    _process_post_element(post_loc, f"{query_str} (week)", None)
                page.evaluate("window.scrollBy(0, 1000)")
                human_delay(1.5, 2.2)
                scroll_count += 1

    # -------------------------------------------------------------------------
    # STAGE 2: Home Feed Fallback / Organic Stream (Zero Search Rate Limits)
    # -------------------------------------------------------------------------
    if total_harvested_this_run < effective_run_limit:
        logging.info("[LinkedIn Harvester] STAGE 2: Scanning LinkedIn Home Feed for recruiter hiring posts (Rate-Limit Free)...")
        feed_stat = query_stats.setdefault("queries", {}).setdefault("Home Feed", {
            "runs": 0,
            "posts_scanned": 0,
            "leads_harvested": 0,
            "emails": []
        })
        feed_stat["runs"] = int(feed_stat.get("runs", 0)) + 1

        try:
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
            human_delay(3.0, 4.5)

            feed_scroll_count = 0
            max_feed_scrolls = min(max_scrolls, 6)
            prev_feed_count = 0
            consecutive_no_new_feed = 0

            while feed_scroll_count < max_feed_scrolls and total_harvested_this_run < effective_run_limit:
                feed_post_locators = page.locator(
                    "div[data-view-name*='feed-full-update'], div.feed-shared-update-v2, "
                    "div[data-urn*='urn:li:activity'], article[data-activity-urn]"
                ).all()

                current_feed_count = len(feed_post_locators)
                logging.debug(f"[LinkedIn Harvester] Feed Scroll {feed_scroll_count+1}/{max_feed_scrolls}: Found {current_feed_count} feed updates.")

                for post_loc in feed_post_locators:
                    if total_harvested_this_run >= effective_run_limit:
                        break
                    _process_post_element(post_loc, "Home Feed", feed_stat)

                page.evaluate("window.scrollBy(0, 1100)")
                human_delay(1.8, 2.8)
                feed_scroll_count += 1

                if current_feed_count == prev_feed_count:
                    consecutive_no_new_feed += 1
                    if consecutive_no_new_feed >= 2:
                        break
                else:
                    consecutive_no_new_feed = 0
                prev_feed_count = current_feed_count

        except Exception as e:
            logging.warning(f"[LinkedIn Harvester] Home Feed scanning encountered an issue: {e}")

    save_linkedin_query_stats(logs_dir, query_stats)
    logging.info(f"[LinkedIn Harvester] Harvest finished. Collected {total_harvested_this_run} new recruiter leads.")
    return total_harvested_this_run


# -------------------------------------------------------------------------
# ENGINE 2: 1-CLICK EASY APPLY BOT
# -------------------------------------------------------------------------

def dismiss_linkedin_modal(page):
    """Cleanly dismiss any open Easy Apply dialog."""
    try:
        dismiss_selectors = [
            "button[aria-label='Dismiss']",
            "button[data-control-name='discard_application_confirm_btn']",
            "button:has-text('Discard')",
            "button:has-text('Cancel')"
        ]
        for d_sel in dismiss_selectors:
            loc = page.locator(d_sel).first
            if loc.count() > 0 and loc.is_visible(timeout=300):
                loc.click(timeout=1000)
                human_delay(0.3, 0.6)
        page.keyboard.press("Escape")
    except Exception:
        pass


def solve_easy_apply_step(page, config):
    """
    Handle common screening questions on LinkedIn Easy Apply steps.
    Uses config.json qa_bank values.
    """
    qa_bank = config.get('naukri', {}).get('qa_bank', {})
    tot_exp = qa_bank.get('total_experience_years', '3.5')
    rel_exp = qa_bank.get('relevant_experience_years', '3')
    curr_ctc = qa_bank.get('current_ctc_lpa', '12')
    exp_ctc = qa_bank.get('expected_ctc_lpa', '15')
    notice = qa_bank.get('notice_period_days', '7')
    phone = config.get('profile', {}).get('phone', '+91-9876543210')

    try:
        # 1. Fill numeric experience/salary inputs
        text_inputs = page.locator("div.jobs-easy-apply-modal input[type='text'], div.jobs-easy-apply-modal input[type='number']").all()
        for inp in text_inputs:
            val = inp.input_value()
            if val and len(val.strip()) > 0:
                continue
            label_text = ""
            try:
                parent = inp.locator("xpath=..").first
                label_text = (parent.inner_text(timeout=200) or "").lower()
            except Exception:
                pass

            if "phone" in label_text or "mobile" in label_text:
                inp.fill(phone)
            elif "experience" in label_text or "years" in label_text:
                inp.fill(rel_exp)
            elif "notice" in label_text:
                inp.fill(notice)
            elif "current" in label_text and ("ctc" in label_text or "salary" in label_text):
                inp.fill(curr_ctc)
            elif "expected" in label_text and ("ctc" in label_text or "salary" in label_text):
                inp.fill(exp_ctc)

        # 2. Radio questions (Work authorization, Relocation, Commute, Yes/No)
        yes_radios = page.locator("div.jobs-easy-apply-modal input[type='radio'][value='Yes'], div.jobs-easy-apply-modal label:has-text('Yes')").all()
        for yr in yes_radios:
            try:
                yr.check(timeout=400)
            except Exception:
                pass

        # 3. Dropdowns
        selects = page.locator("div.jobs-easy-apply-modal select").all()
        for sel in selects:
            try:
                options = sel.locator("option").all_inner_texts()
                for opt in options:
                    if any(w in opt.lower() for w in ("yes", "immediate", "7", "15", "notice")):
                        sel.select_option(label=opt)
                        break
            except Exception:
                pass

    except Exception as e:
        logging.debug(f"[LinkedIn Easy Apply] Form solver note: {e}")


def run_linkedin_easy_apply(context, page, config, ledger, logs_dir, max_applies_override=None):
    """
    Search and apply to matching LinkedIn jobs with Easy Apply:
      1. Filters: Easy Apply (f_AL=true), Past 24h (f_TPR=r86400), Sort by Date (sortBy=DD).
      2. Step-by-step form completion.
      3. Strict daily apply cap (20/day) with human-like delays.
    """
    li_cfg = config.get('linkedin', {})
    search_cfg = li_cfg.get('search', {})
    daily_limit = int(li_cfg.get('daily_apply_limit', 20))
    session_limit = int(max_applies_override) if max_applies_override is not None else daily_limit

    applies_today = count_linkedin_applies_today(ledger)
    logging.info(f"[LinkedIn Easy Apply] Status today: {applies_today}/{daily_limit} applied (Session limit: {session_limit}).")

    if applies_today >= daily_limit and max_applies_override is None:
        logging.info("[LinkedIn Easy Apply] Daily limit reached. Skipping further applications today.")
        return 0

    queries = search_cfg.get('queries', ["Java Full Stack Developer", "Spring Boot Microservices Developer"])
    locations = search_cfg.get('locations', ["Remote", "India", "Bengaluru", "Hyderabad", "Pune"])
    applied_this_run = 0
    applied_ledger = ledger.setdefault("applied_jobs", {})

    for query in queries:
        if applied_this_run >= session_limit or (applies_today + applied_this_run) >= daily_limit:
            break

        for loc in locations:
            if applied_this_run >= session_limit or (applies_today + applied_this_run) >= daily_limit:
                break

            logging.info(f"[LinkedIn Job Search] Searching: '{query}' in '{loc}' (Sort by Date, Past Week)...")
            q_enc = urllib.parse.quote(query)
            l_enc = urllib.parse.quote(loc)
            
            time_filter = search_cfg.get('time_filter', 'past-week')
            tpr_val = 'r604800' if time_filter == 'past-week' else 'r86400'
            search_all = search_cfg.get('search_all_jobs', True)
            al_param = "" if search_all else "f_AL=true&"
            search_url = f"https://www.linkedin.com/jobs/search/?{al_param}f_TPR={tpr_val}&keywords={q_enc}&location={l_enc}&sortBy=DD"

            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                human_delay(3.0, 4.5)
            except Exception as e:
                logging.warning(f"[LinkedIn Job Search] Navigation warning: {e}")
                continue

            # Dynamic virtual scroll to populate all job cards in the left pane
            for _ in range(4):
                try:
                    page.evaluate("""
                        const list = document.querySelector('.jobs-search-results-list') 
                                  || document.querySelector('.scaffold-layout__list') 
                                  || document.querySelector('div[tabindex="-1"]');
                        if (list) list.scrollBy(0, 750);
                    """)
                    time.sleep(0.6)
                except Exception:
                    pass

            # Find job cards in left pane with wait and fallback
            card_sel = (
                "li.scaffold-layout__list-item, div.job-card-container, "
                "div[data-job-id], li.jobs-search-results__list-item, div.base-card, "
                "div.job-card-list__entity-lockup"
            )
            try:
                page.wait_for_selector(card_sel, timeout=5000)
            except Exception:
                pass
            job_cards = page.locator(card_sel).all()

            if len(job_cards) == 0:
                fallback_url = f"https://www.linkedin.com/jobs/search/?keywords={q_enc}&location={l_enc}&sortBy=DD"
                try:
                    page.goto(fallback_url, wait_until="domcontentloaded", timeout=35000)
                    human_delay(2.5, 4.0)
                    try:
                        page.wait_for_selector(card_sel, timeout=4000)
                    except Exception:
                        pass
                    job_cards = page.locator(card_sel).all()
                except Exception:
                    pass

            logging.info(f"[LinkedIn Job Search] Found {len(job_cards)} job cards for '{query}' in '{loc}'.")

            for j_idx, card in enumerate(job_cards):
                if applied_this_run >= session_limit or (applies_today + applied_this_run) >= daily_limit:
                    break

                try:
                    dismiss_linkedin_modal(page)

                    if not card.is_visible(timeout=500):
                        continue

                    # Click job card
                    card.click(timeout=2500)
                    human_delay(1.5, 2.5)

                    # Extract details from details pane
                    title = ""
                    company = ""
                    title_loc = page.locator(".jobs-unified-top-card__job-title, .job-details-jobs-unified-top-card__job-title, h1").first
                    if title_loc.count() > 0:
                        title = (title_loc.inner_text(timeout=400) or "").strip()

                    comp_loc = page.locator(".jobs-unified-top-card__company-name, .job-details-jobs-unified-top-card__company-name, .jobs-unified-top-card__primary-description a").first
                    if comp_loc.count() > 0:
                        company = (comp_loc.inner_text(timeout=400) or "").strip()

                    job_key = f"{company}_{title}".strip("_")
                    if not job_key or job_key in applied_ledger:
                        continue

                    # Filter out excluded titles
                    lower_title = title.lower()
                    if any(ex in lower_title for ex in ("c#", ".net", "php", "wordpress", "sdet", "qa", "manual tester")):
                        continue

                    # Check for Easy Apply button
                    easy_apply_btn = page.locator("button.jobs-apply-button:has-text('Easy Apply'), button:has-text('Easy Apply')").first
                    is_easy_apply = (easy_apply_btn.count() > 0 and easy_apply_btn.is_visible(timeout=1000))

                    if is_easy_apply:
                        logging.info(f"[LinkedIn Easy Apply] Clicking Easy Apply for '{title}' at '{company}'...")
                        easy_apply_btn.click(timeout=2500)
                        human_delay(1.5, 2.5)

                        # Multi-step solver loop (up to 6 steps)
                        submitted = False
                        for _ in range(6):
                            solve_easy_apply_step(page, config)
                            human_delay(0.6, 1.2)

                            # Check for Submit application button
                            submit_btn = page.locator("button:has-text('Submit application'), button[aria-label*='Submit application']").first
                            if submit_btn.count() > 0 and submit_btn.is_visible(timeout=800):
                                submit_btn.click(timeout=3000)
                                human_delay(2.0, 3.5)
                                submitted = True
                                break

                            # Check for Review button
                            review_btn = page.locator("button:has-text('Review'), button[aria-label*='Review your application']").first
                            if review_btn.count() > 0 and review_btn.is_visible(timeout=800):
                                review_btn.click(timeout=2000)
                                human_delay(1.0, 1.8)
                                continue

                            # Check for Next button
                            next_btn = page.locator("button:has-text('Next'), button[aria-label*='Continue to next step']").first
                            if next_btn.count() > 0 and next_btn.is_visible(timeout=800):
                                next_btn.click(timeout=2000)
                                human_delay(1.0, 1.8)
                                continue

                            # If unhandled complex external step, break
                            break

                        dismiss_linkedin_modal(page)

                        if submitted:
                            applied_this_run += 1
                            success_msg = f"✅ Applied {applied_this_run} times on LinkedIn: '{title}' at '{company}'"
                            print(f"\n{success_msg}\n")
                            logging.info(f"[LinkedIn Easy Apply] {success_msg} ({applies_today + applied_this_run}/{daily_limit} today)")
                            notify_macos("LinkedIn Easy Apply", f"Applied to {title} at {company}")

                            applied_ledger[job_key] = {
                                "title": title,
                                "company": company,
                                "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "status": "APPLIED"
                            }
                            save_linkedin_ledger(logs_dir, ledger)
                            human_delay(2.5, 4.5)

                    else:
                        # Check for External Apply button (Workday, Greenhouse, Google Forms, Company ATS)
                        ext_btn = page.locator("button.jobs-apply-button, a.jobs-apply-button, button:has-text('Apply'), a:has-text('Apply')").first
                        if ext_btn.count() > 0 and ext_btn.is_visible(timeout=1000):
                            try:
                                ext_href = ext_btn.get_attribute("href") or ""
                                if not ext_href or ext_href.startswith("#") or "linkedin.com" in ext_href:
                                    try:
                                        with context.expect_page(timeout=3500) as new_page_info:
                                            ext_btn.click(timeout=2000)
                                        new_p = new_page_info.value
                                        new_p.wait_for_load_state("domcontentloaded", timeout=6000)
                                        ext_href = new_p.url
                                        new_p.close()
                                    except Exception:
                                        pass

                                if ext_href:
                                    import ats_bridge
                                    ats_type, ats_url = ats_bridge.find_ats_url_in_text(ext_href, resolve_shorteners=True)
                                    if ats_type:
                                        logging.info(f"[LinkedIn -> ATS Bridge] 🎯 Sourced {ats_type.upper()} external apply: {ats_url}")
                                        ats_bridge.queue_ats_job_to_dropzone(
                                            ats_type=ats_type,
                                            url=ats_url,
                                            title=title,
                                            company=company,
                                            source="LinkedIn",
                                            config=config
                                        )
                                        applied_this_run += 1
                                        applied_ledger[job_key] = {
                                            "title": title,
                                            "company": company,
                                            "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                            "status": f"QUEUED_{ats_type.upper()}",
                                            "ats_url": ats_url
                                        }
                                        save_linkedin_ledger(logs_dir, ledger)
                                        notify_macos(f"LinkedIn -> {ats_type.upper()}", f"Routed {title} at {company}")
                                        print(f"\n🎯 [LinkedIn -> {ats_type.upper()}] Routed '{title}' at '{company}' -> {ats_url}\n")
                            except Exception as ext_err:
                                logging.debug(f"[LinkedIn External Apply] Could not capture external link: {ext_err}")

                except Exception as e:
                    logging.debug(f"[LinkedIn Easy Apply] Error processing card: {e}")
                    dismiss_linkedin_modal(page)

    logging.info(f"[LinkedIn Easy Apply] Run finished. Applied to {applied_this_run} jobs.")
    return applied_this_run


# -------------------------------------------------------------------------
# ENGINE 3: HIRING MANAGER & AGENCY RECRUITER CONNECTION OUTREACH
# -------------------------------------------------------------------------

def is_hiring_relevant(headline, title="", snippet=""):
    """
    Check if a candidate's profile/headline exhibits active hiring intent
    and matches our target software development domain.
    """
    combined = f"{headline} {title} {snippet}".lower()

    # 1. Negative filters: definitely not relevant
    disqualifiers = [
        "student", "looking for job", "looking for opportunity", "seeking opportunity",
        "open to work", "#opentowork", "unemployed", "fresher", "intern at",
        "medical", "nurse", "civil engineer", "real estate"
    ]
    if any(dq in combined for dq in disqualifiers):
        return False

    # 2. Hiring intent markers
    hiring_markers = [
        "#hiring", "hiring", "we're hiring", "we are hiring", "i'm hiring", "i am hiring",
        "talent acquisition", "technical recruiter", "it recruiter", "tech recruiter",
        "recruiter", "recruitment consultant", "staffing specialist", "talent partner",
        "talent lead", "talent specialist", "talent sourcer", "talent scout",
        "growing the team", "building our team", "building the team", "expanding our team",
        "looking for java", "looking for developer", "looking for engineer", "head of engineering",
        "engineering manager", "lead engineer", "tech lead"
    ]
    has_hiring_marker = any(m in combined for m in hiring_markers)
    if not has_hiring_marker:
        return False

    # 3. If recruiter or staffing, accept if they touch tech/IT/software/systems
    recruiter_markers = ["recruiter", "talent", "staffing", "human resources", "talent acquisition"]
    is_recruiter = any(r in combined for r in recruiter_markers)
    if is_recruiter:
        tech_markers = [
            "tech", "it", "software", "java", "developer", "engineer", "cloud", "full stack",
            "backend", "consulting", "solutions", "digital", "systems"
        ]
        return any(t in combined for t in tech_markers)

    # 4. If engineering lead/manager, verify they mention hiring or have hiring badge
    lead_markers = ["engineering manager", "tech lead", "lead engineer", "director of engineering", "cto", "vp of engineering"]
    is_lead = any(l in combined for l in lead_markers)
    if is_lead and any(h in combined for h in ["hiring", "#hiring", "growing", "building", "team"]):
        return True

    return False


def craft_casual_note(candidate, config):
    """
    Craft a friendly, completely non-pushy connection note strictly under 180 characters.
    Zero asks for referrals, interviews, or resume reviews.
    """
    templates = config.get("linkedin", {}).get("connection_outreach", {}).get("templates", {})
    category = candidate.get("category", "post_author")

    raw_name = candidate.get("name", "").strip()
    first_name = raw_name.split()[0] if raw_name else "there"
    first_name = re.sub(r'[^\w\s]', '', first_name).strip() or "there"

    raw_company = candidate.get("company", "").strip()
    company = raw_company if raw_company and len(raw_company) <= 25 else "your company"

    if category == "agency_recruiter":
        template = templates.get(
            "agency_recruiter",
            "Hi {first_name}, saw you specialize in tech recruitment. As a Java & Full Stack dev, would love to connect and stay on your radar for future openings. Cheers!"
        )
    elif category == "hiring_manager":
        template = templates.get(
            "hiring_manager",
            "Hi {first_name}, noticed your engineering work at {company}. Always keen to connect with fellow backend & cloud engineering folks. Best, Amrit"
        )
    else:  # post_author or default
        template = templates.get(
            "post_author",
            "Hi {first_name}, came across your recent tech hiring post. Would love to connect and stay in touch as fellow tech professionals. Cheers, Amrit"
        )

    try:
        note = template.format(first_name=first_name, company=company)
    except Exception:
        note = f"Hi {first_name}, would love to connect with fellow tech professionals. Cheers, Amrit"

    # Enforce strict 180-char cap
    if len(note) > 180:
        if category == "agency_recruiter":
            note = f"Hi {first_name}, saw your focus on tech recruitment. Would love to connect and stay on your radar for Java & Full Stack roles. Cheers, Amrit"
        elif category == "hiring_manager":
            note = f"Hi {first_name}, noticed your work in engineering. Keen to connect with fellow backend & cloud folks. Best, Amrit"
        else:
            note = f"Hi {first_name}, saw your recent hiring post. Would love to connect with fellow tech folks. Cheers, Amrit"

    if len(note) > 180:
        note = note[:177] + "..."

    return note


def get_target_hiring_companies(config, logs_dir, limit=15):
    """
    Retrieve priority target hiring companies from:
      1. Configured companies (config.json -> linkedin.connection_outreach.target_hiring_companies)
      2. Dynamically extracted companies applied to across portal ledgers (Naukri, Indeed, Foundit, Wellfound, Workday)
    """
    import glob
    outreach_cfg = config.get("linkedin", {}).get("connection_outreach", {})
    configured = outreach_cfg.get("target_hiring_companies", [
        "Salesforce", "Target", "Autodesk", "Nvidia", "Adobe",
        "Walmart Global Tech", "Cisco", "Qualcomm", "JPMorganChase",
        "Morgan Stanley", "Goldman Sachs", "Capco", "Shell"
    ])
    companies = [c.strip() for c in configured if c and c.strip()]

    if outreach_cfg.get("auto_target_applied_companies", True):
        applied_cos = []
        for fpath in glob.glob(os.path.join(logs_dir, "*applied*.json")):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    records = d.get("applied_jobs", d.get("applied_forms", []))
                    if isinstance(records, dict):
                        records = list(records.values())
                    for r in sorted(records, key=lambda x: str(x.get("applied_at", "")), reverse=True):
                        if isinstance(r, dict):
                            co = r.get("company", "").strip()
                            if co and len(co) > 2 and co.lower() not in ("confidential", "company", "unknown") and co not in companies and co not in applied_cos:
                                applied_cos.append(co)
            except Exception:
                pass
        companies.extend(applied_cos)

    return companies[:limit]


def get_target_recruitment_agencies(config, limit=10):
    """
    Retrieve priority IT & tech staffing recruitment agencies from:
      config.json -> linkedin.connection_outreach.target_recruitment_agencies
    """
    outreach_cfg = config.get("linkedin", {}).get("connection_outreach", {})
    configured = outreach_cfg.get("target_recruitment_agencies", [
        "Michael Page", "Randstad India", "TEKsystems", "Allegis Group",
        "Adecco", "Kelly Services", "CIEL HR", "ABC Consultants", "Superior Group"
    ])
    return [a.strip() for a in configured if a and a.strip()][:limit]


def gather_outreach_candidates(page, config, ledger, conn_ledger, logs_dir, max_needed=5):
    """
    Gather qualified, hiring-verified candidates from:
      1. Harvested 24h recruiter posts (priority 1: 100% verified active hiring)
      2. Agency recruiter search (priority 2: staffing & tech recruiters)
      3. Applied companies engineering managers (priority 3)
    """
    candidates = []
    seen_urls = set(conn_ledger.get("connections", {}).keys())

    # --- Source 0: Lead Hunt Queue from Screenshots (No-Email Screenshots) ---
    hunt_path = os.path.join(logs_dir, 'lead_hunt_queue.json')
    if os.path.exists(hunt_path):
        try:
            with open(hunt_path, 'r', encoding='utf-8') as f:
                hunt_data = json.load(f)
            hunt_targets = hunt_data.get('targets', [])
            for target in hunt_targets:
                if len(candidates) >= max_needed:
                    break
                if target.get('status') != 'PENDING_HUNT':
                    continue
                co_name = target.get('company_name', '').strip()
                rec_hint = target.get('recruiter_hint', '').strip()
                if not co_name and not rec_hint:
                    continue

                h_query = f'"{rec_hint}" "{co_name}"' if (rec_hint and co_name) else (f'"{co_name}" "Technical Recruiter"' if co_name else f'"{rec_hint}" Recruiter')
                if page:
                    try:
                        logging.info(f"[LinkedIn Outreach] Hunting recruiter from screenshot: {h_query}")
                        encoded_h = urllib.parse.quote(h_query)
                        search_url = f"https://www.linkedin.com/search/results/people/?keywords={encoded_h}&origin=GLOBAL_SEARCH_HEADER"
                        page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                        human_delay(2.5, 4.0)
                        main_loc = page.locator("main").first
                        h_links = main_loc.locator("a[href*='/in/']").all()
                        for h_l in h_links:
                            h_href = h_l.get_attribute("href") or ""
                            if not h_href or "/in/" not in h_href:
                                continue
                            h_clean = h_href.split("?")[0].rstrip("/") + "/"
                            if h_clean in seen_urls:
                                continue
                            h_text = h_l.inner_text().strip().replace("\n", " ")
                            if not h_text or "linkedin member" in h_text.lower():
                                continue
                            candidates.append({
                                "profile_url": h_clean,
                                "name": h_text.split("•")[0].strip(),
                                "headline": f"Recruiter at {co_name}",
                                "company": co_name,
                                "category": "hiring_manager",
                                "source": "screenshot_lead_hunt",
                                "source_key": target.get("id")
                            })
                            seen_urls.add(h_clean)
                            target['status'] = 'HUNTED'
                            with open(hunt_path, 'w', encoding='utf-8') as f_out:
                                json.dump(hunt_data, f_out, indent=2)
                            logging.info(f"[LinkedIn Outreach] 🎯 Sourced screenshot hunt lead: {h_clean} ({co_name})")
                            break
                    except Exception as e:
                        logging.debug(f"[LinkedIn Outreach] Hunt search error for {h_query}: {e}")
        except Exception as e:
            logging.debug(f"[LinkedIn Outreach] Could not process lead hunt queue: {e}")

    # --- Source 1: Harvested Recruiter Post Authors ---
    harvested = ledger.get("harvested_posts", {})
    for email_key, post_info in harvested.items():
        if len(candidates) >= max_needed:
            break
        if not isinstance(post_info, dict):
            continue
        author_url = (post_info.get("author_profile") or "").strip()
        if not author_url or "linkedin.com/in/" not in author_url:
            continue

        clean_url = author_url.split("?")[0].rstrip("/") + "/"
        if clean_url in seen_urls:
            continue

        author_name = post_info.get("author", "").strip()
        headline = post_info.get("headline", "").strip()

        if "/company/" in author_url or not author_name or "@" in author_name:
            continue

        category = "agency_recruiter" if any(w in headline.lower() for w in ["recruiter", "staffing", "talent", "hr"]) else "post_author"
        company = post_info.get("company", "") or ""

        candidates.append({
            "profile_url": clean_url,
            "name": author_name,
            "headline": headline,
            "company": company,
            "category": category,
            "source": "harvested_post",
            "source_key": email_key
        })
        seen_urls.add(clean_url)

    logging.info(f"[LinkedIn Outreach] Sourced {len(candidates)} candidates from harvested posts.")

    if len(candidates) >= max_needed:
        return candidates[:max_needed]

    # --- Source 2: Agency Recruiters via Search ---
    agency_queries = config.get("linkedin", {}).get("connection_outreach", {}).get("agency_search_queries", [
        '"Technical Recruiter" Java India',
        '"IT Recruiter" Java India',
        '"Staffing Specialist" Java India'
    ])
    if agency_queries and page:
        for query in agency_queries:
            if len(candidates) >= max_needed:
                break
            try:
                logging.info(f"[LinkedIn Outreach] Searching agency recruiters: {query}")
                encoded_q = urllib.parse.quote(query)
                search_url = f"https://www.linkedin.com/search/results/people/?keywords={encoded_q}&origin=GLOBAL_SEARCH_HEADER"
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                human_delay(3.0, 5.0)

                # Find profile links in the search results
                main_loc = page.locator("main").first
                links = main_loc.locator("a[href*='/in/']").all()
                logging.info(f"[LinkedIn Outreach] Found {len(links)} profile link elements in search.")

                for link in links:
                    if len(candidates) >= max_needed:
                        break
                    try:
                        href = link.get_attribute("href") or ""
                        if not href or "/in/" not in href or "/opportunities/" in href or "/detail/" in href:
                            continue
                        clean_url = href.split("?")[0].rstrip("/") + "/"
                        if clean_url in seen_urls:
                            continue

                        link_text = link.inner_text().strip().replace("\n", " ")
                        if link_text.lower() in ("see open roles", "view profile", "connect", "follow"):
                            continue

                        # Check card text for relationship distance
                        card_text = ""
                        try:
                            card_text = link.locator("xpath=./ancestor::div[3]").inner_text().strip().replace("\n", " ")
                        except Exception:
                            pass

                        # Skip if already 1st degree connected
                        if "• 1st" in link_text or "• 1st" in card_text or "· 1st" in card_text:
                            continue

                        # Extract name and headline
                        combined_text = f"{link_text} {card_text}"
                        if not is_hiring_relevant(combined_text):
                            continue

                        name_candidate = link_text.split("•")[0].strip()
                        if not name_candidate or len(name_candidate) > 40 or name_candidate.lower() in ("see open roles", "view profile", "connect"):
                            # Try to extract clean name from card_text
                            first_part = card_text.split("•")[0].strip() if card_text else ""
                            name_candidate = first_part if (first_part and len(first_part) < 40) else (link_text.split()[0] if link_text else "Recruiter")

                        headline_candidate = card_text if card_text else link_text

                        candidates.append({
                            "profile_url": clean_url,
                            "name": name_candidate,
                            "headline": headline_candidate,
                            "company": "",
                            "category": "agency_recruiter",
                            "source": "agency_search"
                        })
                        seen_urls.add(clean_url)
                        logging.info(f"[LinkedIn Outreach] Qualified agency recruiter: '{name_candidate}' — {clean_url}")
                    except Exception as card_err:
                        logging.debug(f"[LinkedIn Outreach] Error parsing search link: {card_err}")
            except Exception as search_err:
                logging.warning(f"[LinkedIn Outreach] Search query '{query}' warning: {search_err}")

    # --- Source 3: Target Hiring Companies (Hiring Managers, Engineering Leads & Recruiters) ---
    target_companies = get_target_hiring_companies(config, logs_dir, limit=12)
    if target_companies and page and len(candidates) < max_needed:
        for co in target_companies:
            if len(candidates) >= max_needed:
                break
            try:
                co_query = f'"{co}" ("Technical Recruiter" OR "Engineering Manager" OR "Talent Acquisition" OR "HR") India'
                logging.info(f"[LinkedIn Outreach] Searching hiring managers at target company: {co}")
                encoded_co = urllib.parse.quote(co_query)
                search_url = f"https://www.linkedin.com/search/results/people/?keywords={encoded_co}&origin=GLOBAL_SEARCH_HEADER"
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                human_delay(3.0, 4.5)

                main_loc = page.locator("main").first
                links = main_loc.locator("a[href*='/in/']").all()
                for link in links:
                    if len(candidates) >= max_needed:
                        break
                    try:
                        href = link.get_attribute("href") or ""
                        if not href or "/in/" not in href or "/opportunities/" in href or "/detail/" in href:
                            continue
                        clean_url = href.split("?")[0].rstrip("/") + "/"
                        if clean_url in seen_urls:
                            continue

                        link_text = link.inner_text().strip().replace("\n", " ")
                        if link_text.lower() in ("see open roles", "view profile", "connect", "follow"):
                            continue

                        card_text = ""
                        try:
                            card_text = link.locator("xpath=./ancestor::div[3]").inner_text().strip().replace("\n", " ")
                        except Exception:
                            pass

                        if "• 1st" in link_text or "• 1st" in card_text or "· 1st" in card_text:
                            continue

                        combined_text = f"{link_text} {card_text}"
                        if not is_hiring_relevant(combined_text):
                            continue

                        name_cand = link_text.split("•")[0].strip()
                        if not name_cand or len(name_cand) > 40 or name_cand.lower() in ("see open roles", "view profile", "connect"):
                            first_part = card_text.split("•")[0].strip() if card_text else ""
                            name_cand = first_part if (first_part and len(first_part) < 40) else (link_text.split()[0] if link_text else "Manager")

                        candidates.append({
                            "profile_url": clean_url,
                            "name": name_cand,
                            "headline": card_text if card_text else link_text,
                            "company": co,
                            "category": "hiring_manager",
                            "source": f"target_company:{co}"
                        })
                        seen_urls.add(clean_url)
                        logging.info(f"[LinkedIn Outreach] 🎯 Sourced target company manager: '{name_cand}' at '{co}' — {clean_url}")
                    except Exception:
                        pass
            except Exception as e:
                logging.debug(f"[LinkedIn Outreach] Target company search error for {co}: {e}")

    # --- Source 4: Target Recruitment Agencies ---
    target_agencies = get_target_recruitment_agencies(config, limit=8)
    if target_agencies and page and len(candidates) < max_needed:
        for agency in target_agencies:
            if len(candidates) >= max_needed:
                break
            try:
                ag_query = f'"{agency}" ("Technical Recruiter" OR "IT Recruiter" OR "Staffing Specialist") India'
                logging.info(f"[LinkedIn Outreach] Searching agency recruiters: {agency}")
                encoded_ag = urllib.parse.quote(ag_query)
                search_url = f"https://www.linkedin.com/search/results/people/?keywords={encoded_ag}&origin=GLOBAL_SEARCH_HEADER"
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                human_delay(3.0, 4.5)

                main_loc = page.locator("main").first
                links = main_loc.locator("a[href*='/in/']").all()
                for link in links:
                    if len(candidates) >= max_needed:
                        break
                    try:
                        href = link.get_attribute("href") or ""
                        if not href or "/in/" not in href or "/opportunities/" in href or "/detail/" in href:
                            continue
                        clean_url = href.split("?")[0].rstrip("/") + "/"
                        if clean_url in seen_urls:
                            continue

                        link_text = link.inner_text().strip().replace("\n", " ")
                        if link_text.lower() in ("see open roles", "view profile", "connect", "follow"):
                            continue

                        card_text = ""
                        try:
                            card_text = link.locator("xpath=./ancestor::div[3]").inner_text().strip().replace("\n", " ")
                        except Exception:
                            pass

                        if "• 1st" in link_text or "• 1st" in card_text or "· 1st" in card_text:
                            continue

                        combined_text = f"{link_text} {card_text}"
                        if not is_hiring_relevant(combined_text):
                            continue

                        name_cand = link_text.split("•")[0].strip()
                        if not name_cand or len(name_cand) > 40 or name_cand.lower() in ("see open roles", "view profile", "connect"):
                            first_part = card_text.split("•")[0].strip() if card_text else ""
                            name_cand = first_part if (first_part and len(first_part) < 40) else (link_text.split()[0] if link_text else "Recruiter")

                        candidates.append({
                            "profile_url": clean_url,
                            "name": name_cand,
                            "headline": card_text if card_text else link_text,
                            "company": agency,
                            "category": "agency_recruiter",
                            "source": f"target_agency:{agency}"
                        })
                        seen_urls.add(clean_url)
                        logging.info(f"[LinkedIn Outreach] 🎯 Sourced agency recruiter: '{name_cand}' at '{agency}' — {clean_url}")
                    except Exception:
                        pass
            except Exception as e:
                logging.debug(f"[LinkedIn Outreach] Target agency search error for {agency}: {e}")

    return candidates[:max_needed]


def send_linkedin_connection(page, candidate, note, config, conn_ledger, logs_dir):
    """
    Navigate to a candidate's LinkedIn profile and send a connection request.
    Uses direct custom-invite preload URL for reliable native modal interaction:
      - https://www.linkedin.com/preload/custom-invite/?vanityName={vanity}
    Handles:
      - Add note vs free-tier note quota exhaustion fallback
      - Already pending / already connected detection
      - Weekly platform invitation limits
      - Email challenge detection & bypass
    """
    profile_url = candidate["profile_url"]
    outreach_cfg = config.get("linkedin", {}).get("connection_outreach", {})
    send_without_note = outreach_cfg.get("send_without_note_if_quota_exhausted", True)

    vanity = ""
    if "/in/" in profile_url:
        try:
            vanity = profile_url.split("/in/")[1].split("/")[0].split("?")[0].strip()
        except Exception:
            pass

    if not vanity:
        logging.warning(f"[LinkedIn Outreach] Could not extract vanity name from {profile_url}. Skipping.")
        return False

    invite_url = f"https://www.linkedin.com/preload/custom-invite/?vanityName={vanity}"
    logging.info(f"[LinkedIn Outreach] Opening custom invite endpoint: {invite_url} ({candidate.get('name')})")

    try:
        page.goto(invite_url, wait_until="domcontentloaded", timeout=35000)
    except Exception as e:
        logging.warning(f"[LinkedIn Outreach] Navigation error to {invite_url}: {e}")
        return False

    human_delay(2.5, 4.0)

    # 1. Look for the Native Invitation Dialog
    modal = page.locator("div[role='dialog'], .artdeco-modal").first
    modal_opened = False
    try:
        modal.wait_for(state="visible", timeout=6000)
        modal_opened = True
    except Exception:
        modal_opened = False

    # If modal didn't open on direct preload URL, fall back to profile page inspection
    if not modal_opened:
        logging.info(f"[LinkedIn Outreach] Direct invite modal did not open. Checking profile {profile_url} directly...")
        try:
            page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            human_delay(2.5, 4.0)

            # Check if already pending or 1st degree
            body_text = page.locator("main").inner_text().lower()
            if "pending" in body_text and "invitation" in body_text:
                logging.info(f"[LinkedIn Outreach] Connection already pending for {candidate.get('name')}.")
                conn_ledger.setdefault("connections", {})[profile_url] = {
                    "name": candidate.get("name"),
                    "company": candidate.get("company"),
                    "category": candidate.get("category"),
                    "status": "ALREADY_PENDING",
                    "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                save_linkedin_connections_ledger(logs_dir, conn_ledger)
                return False

            connect_link = page.locator("a[href*='custom-invite'], a[aria-label*='Invite'][aria-label*='to connect']").first
            if connect_link.count() > 0 and connect_link.is_visible():
                connect_link.evaluate("el => el.scrollIntoView({block: 'center'})")
                human_delay(0.8, 1.5)
                connect_link.click(force=True)
                human_delay(2.0, 3.5)
                modal = page.locator("div[role='dialog'], .artdeco-modal").first
                if modal.count() > 0 and modal.is_visible(timeout=4000):
                    modal_opened = True
        except Exception as fb_err:
            logging.debug(f"[LinkedIn Outreach] Profile fallback error: {fb_err}")

    if not modal_opened:
        logging.info(f"[LinkedIn Outreach] Could not open connection modal for {candidate.get('name')}. Skipping.")
        return False

    modal_text = modal.inner_text().lower()

    # Check for email challenge
    if "email" in modal_text and ("enter their email" in modal_text or "verify" in modal_text):
        logging.info(f"[LinkedIn Outreach] Email address required to connect with {candidate.get('name')}. Cancelling.")
        dismiss_btn = modal.locator("button[aria-label='Dismiss'], button:has-text('Cancel')").first
        if dismiss_btn.count() > 0:
            dismiss_btn.click(timeout=2000)
        return False

    # Check for weekly invitation limit on platform
    weekly_limit_markers = [
        "weekly invitation limit",
        "reached the weekly invitation limit",
        "limit for invitations",
        "invitation limit reached",
        "you've reached your weekly limit",
        "out of invitations",
        "invitations cannot be sent",
        "weekly limit"
    ]
    if any(marker in modal_text for marker in weekly_limit_markers):
        logging.warning("[LinkedIn Outreach] ⚠️ LinkedIn weekly invitation limit reached on platform! Engaging 7-day safety lock.")
        restricted_until = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        conn_ledger["platform_restriction"] = {
            "is_restricted": True,
            "restricted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "restricted_until": restricted_until,
            "reason": "Platform weekly invitation limit reached (7-day dynamic cap enforced by LinkedIn)"
        }
        conn_ledger.setdefault("weekly_stats", {})["invites_sent_this_week"] = conn_ledger.get("weekly_stats", {}).get("weekly_cap", 80)
        save_linkedin_connections_ledger(logs_dir, conn_ledger)
        notify_macos("LinkedIn Weekly Limit", "LinkedIn weekly invitation limit reached. Pausing outreach for 7 days to protect account.", is_urgent=True)
        dismiss_btn = modal.locator("button[aria-label='Dismiss'], button:has-text('Got it'), button:has-text('Cancel')").first
        if dismiss_btn.count() > 0:
            try:
                dismiss_btn.click(timeout=2000)
            except Exception:
                pass
        return "WEEKLY_LIMIT_REACHED"

    # Check for free-tier custom note restriction / quota limit
    note_quota_exhausted = (
        "monthly limit" in modal_text or
        "upgrade to premium" in modal_text or
        ("personalized invitations" in modal_text and "0 left" in modal_text)
    )

    note_sent = False
    note_to_record = ""

    add_note_btn = modal.locator("button:has-text('Add a note')").first
    if add_note_btn.count() > 0 and add_note_btn.is_visible(timeout=1000) and not note_quota_exhausted:
        try:
            add_note_btn.click(timeout=2000)
            human_delay(1.0, 1.8)

            textarea = modal.locator("textarea#custom-message, textarea[name='message'], textarea").first
            if textarea.count() > 0 and textarea.is_visible(timeout=1500):
                textarea.fill(note)
                human_delay(0.8, 1.5)
                note_sent = True
                note_to_record = note
                logging.info(f"[LinkedIn Outreach] Personalized note added ({len(note)} chars): '{note}'")
        except Exception as note_err:
            logging.debug(f"[LinkedIn Outreach] Could not add note, falling back: {note_err}")

    if not note_sent and not send_without_note:
        logging.info("[LinkedIn Outreach] Custom note unavailable and send_without_note_if_quota_exhausted is False. Cancelling.")
        dismiss_btn = modal.locator("button[aria-label='Dismiss'], button:has-text('Cancel')").first
        if dismiss_btn.count() > 0:
            dismiss_btn.click(timeout=2000)
        return False

    send_btn = modal.locator("button:has-text('Send'), button:has-text('Send without a note'), button[aria-label*='Send invitation']").first
    if send_btn.count() > 0 and send_btn.is_visible(timeout=1500):
        try:
            send_btn.click(timeout=3000)
            human_delay(1.5, 2.5)
        except Exception as send_err:
            logging.warning(f"[LinkedIn Outreach] Could not click Send button: {send_err}")
            return False

    try:
        modal.wait_for(state="hidden", timeout=4000)
    except Exception:
        pass

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn_ledger.setdefault("connections", {})[profile_url] = {
        "name": candidate.get("name"),
        "headline": candidate.get("headline"),
        "company": candidate.get("company"),
        "category": candidate.get("category"),
        "status": "INVITED",
        "note_sent": note_to_record if note_sent else "[SENT_WITHOUT_NOTE]",
        "sent_at": now_str
    }
    save_linkedin_connections_ledger(logs_dir, conn_ledger)

    log_msg = f"🤝 Sent LinkedIn connection request to {candidate.get('name')} ({candidate.get('category')})"
    print(f"\n{log_msg}\n")
    logging.info(f"[LinkedIn Outreach] {log_msg}")
    notify_macos("LinkedIn Connection", f"Sent request to {candidate.get('name')}")

    delay_range = outreach_cfg.get("safe_delay_range_seconds", [4.0, 7.5])
    min_d = float(delay_range[0]) if isinstance(delay_range, list) and len(delay_range) > 0 else 4.0
    max_d = float(delay_range[1]) if isinstance(delay_range, list) and len(delay_range) > 1 else 7.5
    human_delay(min_d, max_d)
    return True


def send_direct_linkedin_message(page, profile_url, msg_text):
    """
    Navigate to an accepted 1st-degree connection's profile and send a direct LinkedIn message.
    """
    try:
        logging.info(f"[LinkedIn Direct Message] Opening profile: {profile_url}")
        page.goto(profile_url, wait_until="domcontentloaded", timeout=35000)
        human_delay(2.5, 4.0)

        # Look for primary 'Message' button
        msg_btn = page.locator("button.artdeco-button:has-text('Message'), a:has-text('Message'), button[aria-label*='Message']").first
        if msg_btn.count() == 0 or not msg_btn.is_visible(timeout=3000):
            logging.warning(f"[LinkedIn Direct Message] Message button not found on {profile_url}.")
            return False

        msg_btn.click(timeout=3000)
        human_delay(1.5, 2.5)

        # Wait for the chat box / contenteditable textbox
        textbox_sel = "div[role='textbox'], div.msg-form__contenteditable, div[contenteditable='true'], div.msg-form__message-texteditor"
        try:
            page.wait_for_selector(textbox_sel, timeout=5000)
        except Exception:
            pass

        textbox = page.locator(textbox_sel).first
        if textbox.count() == 0 or not textbox.is_visible(timeout=2000):
            logging.warning(f"[LinkedIn Direct Message] Textbox not found for {profile_url}.")
            return False

        # Fill message text
        textbox.click(timeout=2000)
        human_delay(0.5, 1.0)
        page.keyboard.type(msg_text, delay=15)
        human_delay(1.0, 1.8)

        # Locate Send button
        send_btn = page.locator("button.msg-form__send-button, button[type='submit']:has-text('Send'), button:has-text('Send')").first
        if send_btn.count() == 0 or not send_btn.is_visible(timeout=2000):
            page.keyboard.press("Meta+Enter")
        else:
            send_btn.click(timeout=3000)

        human_delay(2.0, 3.5)

        # Close the conversation window if overlay is present
        try:
            close_btn = page.locator("button[data-control-name='overlay.close_conversation_window'], button[aria-label*='Close conversation'], button[aria-label*='close']").first
            if close_btn.count() > 0 and close_btn.is_visible(timeout=1000):
                close_btn.click(timeout=1500)
        except Exception:
            pass

        return True
    except Exception as e:
        logging.warning(f"[LinkedIn Direct Message] Error sending message to {profile_url}: {e}")
        return False


def check_and_message_accepted_connections(context, page, config, conn_ledger, logs_dir):
    """
    1. Visit https://www.linkedin.com/mynetwork/invite-connect/connections/ to detect recently accepted connections.
    2. Update their status from INVITED/SENT to ACCEPTED in linkedin_connections.json.
    3. For accepted connections that haven't received a welcome message yet:
       Send a warm, personalized direct message (referencing their company/recruiter role and candidate's 3+ years Java/Full Stack background).
    4. Persist welcome_message_sent: True to prevent duplicate messages.
    """
    outreach_cfg = config.get("linkedin", {}).get("connection_outreach", {})
    msg_cfg = outreach_cfg.get("post_acceptance_messaging", {})
    if not msg_cfg.get("enabled", True):
        return 0

    max_messages = int(msg_cfg.get("max_messages_per_run", 5))
    conns = conn_ledger.setdefault("connections", {})

    logging.info("[LinkedIn Connections] Checking recently accepted connections on /mynetwork/invite-connect/connections/...")

    # 1. Navigate to connections list
    try:
        page.goto("https://www.linkedin.com/mynetwork/invite-connect/connections/", wait_until="domcontentloaded", timeout=40000)
        human_delay(3.0, 5.0)
    except Exception as e:
        logging.warning(f"[LinkedIn Connections] Navigation to connections page warning: {e}")
        return 0

    # 2. Scan recent 1st-degree connection cards
    newly_accepted = 0
    try:
        card_sel = "li.mn-connection-card, div.mn-connection-card, div[data-view-name='connection-card']"
        cards = page.locator(card_sel).all()
        logging.info(f"[LinkedIn Connections] Found {len(cards)} connection cards on page.")

        for c_idx, card in enumerate(cards[:30]):
            try:
                link_loc = card.locator("a[href*='/in/']").first
                if link_loc.count() == 0:
                    continue
                href = link_loc.get_attribute("href") or ""
                if not href or "/in/" not in href:
                    continue
                clean_url = href.split("?")[0].rstrip("/") + "/"

                matched_key = None
                for tracked_url in conns.keys():
                    if tracked_url in clean_url or clean_url in tracked_url:
                        matched_key = tracked_url
                        break

                if matched_key:
                    c_info = conns[matched_key]
                    if c_info.get("status") in ("INVITED", "SENT"):
                        c_info["status"] = "ACCEPTED"
                        c_info["accepted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        newly_accepted += 1
                        logging.info(f"[LinkedIn Connections] 🎉 Connection accepted by: {c_info.get('name')} ({matched_key})")
                        notify_macos("LinkedIn Connection Accepted", f"{c_info.get('name')} accepted your connection request!")
            except Exception as card_err:
                logging.debug(f"[LinkedIn Connections] Card scan error: {card_err}")

        if newly_accepted > 0:
            save_linkedin_connections_ledger(logs_dir, conn_ledger)
            logging.info(f"[LinkedIn Connections] Discovered {newly_accepted} newly accepted connections!")
    except Exception as e:
        logging.warning(f"[LinkedIn Connections] Error scanning connections: {e}")

    # 3. Send Post-Acceptance Welcome Message to accepted connections
    accepted_to_message = [
        (url, info) for url, info in conns.items()
        if info.get("status") == "ACCEPTED" and not info.get("welcome_message_sent")
    ]

    if not accepted_to_message:
        logging.info("[LinkedIn Connections] No pending welcome messages to send.")
        return newly_accepted

    logging.info(f"[LinkedIn Connections] Found {len(accepted_to_message)} accepted connections awaiting welcome message (Sending up to {max_messages}).")
    templates = msg_cfg.get("templates", {})
    sent_messages_count = 0

    for profile_url, info in accepted_to_message[:max_messages]:
        name = info.get("name") or "there"
        first_name = name.split()[0] if name else "there"
        company = info.get("company") or "your organization"
        category = info.get("category", "default")

        tmpl = templates.get(category) or templates.get("default") or "Hi {first_name}, thanks for connecting! Looking forward to staying in touch."
        msg_text = tmpl.replace("{first_name}", first_name).replace("{company}", company)

        logging.info(f"[LinkedIn Welcome] Preparing message for {name} ({profile_url}):\n'{msg_text[:80]}...'")

        success = send_direct_linkedin_message(page, profile_url, msg_text)
        if success:
            sent_messages_count += 1
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            info["welcome_message_sent"] = True
            info["welcome_message_at"] = now_str
            info["welcome_message_text"] = msg_text
            save_linkedin_connections_ledger(logs_dir, conn_ledger)

            print(f"\n💬 [LinkedIn Welcome Message Sent] To {name} ({company})\n")
            logging.info(f"[LinkedIn Welcome] Successfully sent welcome message to {name}")
            notify_macos("LinkedIn Welcome Sent", f"Sent message to {name}")
            human_delay(3.5, 6.0)

    return sent_messages_count


def run_linkedin_connection_outreach(context, page, config, ledger, logs_dir, max_connect_override=None):
    """
    Execute daily LinkedIn connection outreach targeting:
      - Active recruiter post authors from past 24h (Priority 1)
      - Technical & IT recruitment agency consultants (Priority 2)
      - Engineering Managers at applied companies (Priority 3)
    Enforces dynamic LinkedIn limits based on account tier:
      - Standard/Free: 80 safe / 100 max weekly rolling limit, 12 daily cap
      - 7-Day platform restriction safety auto-lock
      - 30,000 hard first-degree connection cap
      - 30% acceptance health monitoring
    """
    outreach_cfg = config.get("linkedin", {}).get("connection_outreach", {})
    if not outreach_cfg.get("enabled", True):
        logging.info("[LinkedIn Outreach] Connection outreach is disabled in config.json.")
        return 0

    conn_ledger = load_linkedin_connections_ledger(logs_dir)

    # 0. Check for newly accepted connections & send post-acceptance welcome messages
    try:
        check_and_message_accepted_connections(context, page, config, conn_ledger, logs_dir)
    except Exception as e:
        logging.warning(f"[LinkedIn Outreach] Error checking accepted connections: {e}")

    # 1. Platform restriction check (7-day safety lock)
    is_res, res_until = is_platform_restriction_active(conn_ledger)
    if is_res:
        msg = f"[LinkedIn Outreach] ⚠️ LinkedIn 7-day platform invitation block is ACTIVE until {res_until}. Skipping connection outreach to prevent account suspension."
        logging.warning(msg)
        print(f"\n{msg}\n")
        return 0

    # 2. Hard network cap check (30,000 connections)
    hard_network_cap = int(outreach_cfg.get("hard_network_cap", 30000))
    known_1st_degree = conn_ledger.get("first_degree_connection_count", 0)
    if known_1st_degree >= (hard_network_cap - 50):
        msg = f"[LinkedIn Outreach] 🛑 Reached or approaching LinkedIn 30,000 hard network cap ({known_1st_degree}/{hard_network_cap}). Halting."
        logging.warning(msg)
        print(f"\n{msg}\n")
        return 0

    # 3. Dynamic caps based on account tier
    account_tier = str(outreach_cfg.get("account_tier", "standard")).lower()
    tier_defaults = {
        "standard": {"daily": 12, "weekly": 80, "max_weekly": 100},
        "sales_navigator": {"daily": 25, "weekly": 150, "max_weekly": 200},
        "new_account": {"daily": 6, "weekly": 40, "max_weekly": 50}
    }
    tier_info = tier_defaults.get(account_tier, tier_defaults["standard"])

    platform_max_weekly = int(outreach_cfg.get("platform_max_weekly_limit", tier_info["max_weekly"]))
    daily_cap = int(outreach_cfg.get("daily_limit", tier_info["daily"]))
    weekly_cap = min(int(outreach_cfg.get("weekly_limit", tier_info["weekly"])), platform_max_weekly)

    if max_connect_override is not None:
        daily_cap = max_connect_override

    # Check weekly limits (rolling 7 days)
    sent_last_7_days = count_connections_last_7_days(conn_ledger)
    logging.info(
        f"[LinkedIn Outreach] Weekly check ({account_tier.title()} Tier): "
        f"{sent_last_7_days} / {weekly_cap} sent in last 7 days (Platform Max: {platform_max_weekly})."
    )
    if sent_last_7_days >= weekly_cap:
        msg = (
            f"[LinkedIn Outreach] Weekly connection cap reached ({sent_last_7_days}/{weekly_cap} safe cap, "
            f"Platform Max: {platform_max_weekly}). Halting to protect account reputation."
        )
        print(f"\n{msg}\n")
        logging.info(msg)
        return 0

    # Check daily limits
    sent_today = count_connections_today(conn_ledger)
    logging.info(f"[LinkedIn Outreach] Daily check: {sent_today} / {daily_cap} sent today.")
    if sent_today >= daily_cap:
        msg = f"[LinkedIn Outreach] Daily connection cap reached ({sent_today}/{daily_cap}). Will resume tomorrow."
        print(f"\n{msg}\n")
        logging.info(msg)
        return 0

    remaining_daily = daily_cap - sent_today
    remaining_weekly = weekly_cap - sent_last_7_days
    max_to_send = min(remaining_daily, remaining_weekly)

    # 4. Acceptance rate and quality monitoring
    min_acceptance_warning = float(outreach_cfg.get("min_acceptance_rate_warning", 0.30))
    check_and_log_acceptance_health(conn_ledger, min_acceptance_warning)

    logging.info(f"[LinkedIn Outreach] Finding up to {max_to_send} qualified hiring candidates...")
    candidates = gather_outreach_candidates(page, config, ledger, conn_ledger, logs_dir, max_needed=max_to_send)
    if not candidates:
        logging.info("[LinkedIn Outreach] No new qualified candidates found to connect with.")
        return 0

    sent_count = 0
    for cand in candidates:
        if sent_count >= max_to_send:
            break
        note = craft_casual_note(cand, config)
        res = send_linkedin_connection(page, cand, note, config, conn_ledger, logs_dir)
        if res == "WEEKLY_LIMIT_REACHED":
            logging.warning("[LinkedIn Outreach] Platform weekly invitation limit reached during outreach. Halting immediately.")
            break
        elif res is True:
            sent_count += 1

    # Update ledger stats
    conn_ledger.setdefault("weekly_stats", {})["invites_sent_this_week"] = count_connections_last_7_days(conn_ledger)
    conn_ledger["weekly_stats"]["weekly_cap"] = weekly_cap
    conn_ledger["weekly_stats"]["platform_max_limit"] = platform_max_weekly
    conn_ledger.setdefault("daily_stats", {})["invites_sent_today"] = count_connections_today(conn_ledger)
    conn_ledger["daily_stats"]["daily_cap"] = daily_cap
    save_linkedin_connections_ledger(logs_dir, conn_ledger)

    logging.info(f"[LinkedIn Outreach] Completed run. Sent {sent_count} connection requests.")
    return sent_count


# -------------------------------------------------------------------------
# MAIN PIPELINE RUNNER
# -------------------------------------------------------------------------

def run_linkedin_pipeline(mode="all", max_applies=None, max_harvest=None, max_connect=None, force_headed=False, interactive_login=True):
    """Main entry point for LinkedIn automation pipeline."""
    import fcntl
    lock_path = "/tmp/linkedin_automation_agent.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, IOError):
        print("Another LinkedIn Automation instance is already running. Skipping concurrent execution.")
        return False

    config = email_agent.load_config()
    paths = config.get('paths', {})
    logs_dir = os.path.expanduser(paths.get('logs', '~/Documents/JobAutomation/logs'))
    email_agent.setup_logging(logs_dir)

    li_cfg = config.get('linkedin', {})
    if not li_cfg.get('enabled', True):
        logging.info("[LinkedIn] LinkedIn automation is disabled in config.json.")
        return False

    ledger = load_linkedin_ledger(logs_dir)
    if mode in ("all", "apply", "harvest", "connect", "outreach"):
        ledger["last_daily_auto_run_date"] = datetime.now().strftime("%Y-%m-%d")
        save_linkedin_ledger(logs_dir, ledger)

    with sync_playwright() as p:
        context = launch_linkedin_browser(p, config, force_headed=force_headed or (mode == "login"))
        try:
            page = context.pages[0] if context.pages else context.new_page()

            wait_secs = 300 if (interactive_login or mode == "login") else 15
            if not ensure_logged_in(page, interactive_wait_seconds=wait_secs):
                logging.error("[LinkedIn] Cannot proceed without an authenticated LinkedIn session. Run: python3 linkedin_agent.py --mode login")
                return False

            ledger["login_verified"] = True
            save_linkedin_ledger(logs_dir, ledger)

            if mode == "login":
                logging.info("[LinkedIn] Login verification complete! Persistent profile is ready.")
                print("\n[SUCCESS] LinkedIn session verified and persistent Chrome profile is saved!\n")
                return True

            if mode == "check_accepted":
                try:
                    conn_ledger = load_linkedin_connections_ledger(logs_dir)
                    check_and_message_accepted_connections(context, page, config, conn_ledger, logs_dir)
                except Exception as e:
                    logging.error(f"[LinkedIn] Error checking accepted connections: {e}", exc_info=True)
                return True

            if mode in ("all", "harvest"):
                try:
                    harvest_recruiter_posts(page, config, ledger, logs_dir, max_harvest_override=max_harvest)
                except Exception as e:
                    logging.error(f"[LinkedIn] Recruiter post harvester encountered error: {e}", exc_info=True)

            if mode in ("all", "apply"):
                try:
                    run_linkedin_easy_apply(context, page, config, ledger, logs_dir, max_applies_override=max_applies)
                except Exception as e:
                    logging.error(f"[LinkedIn] Easy Apply bot encountered error: {e}", exc_info=True)

            if mode in ("all", "connect", "outreach"):
                try:
                    run_linkedin_connection_outreach(context, page, config, ledger, logs_dir, max_connect_override=max_connect)
                except Exception as e:
                    logging.error(f"[LinkedIn] Connection outreach encountered error: {e}", exc_info=True)

            ledger["last_daily_auto_run_date"] = datetime.now().strftime("%Y-%m-%d")
            save_linkedin_ledger(logs_dir, ledger)
            return True

        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                lock_file.close()
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LinkedIn Autonomous Recruiter Post Harvester, 1-Click Easy Apply & Connection Outreach Agent")
    parser.add_argument(
        "--mode",
        choices=["all", "login", "harvest", "apply", "connect", "outreach", "check_accepted"],
        default="all",
        help="Execution mode: 'login', 'harvest', 'apply', 'connect', 'check_accepted', or 'all'."
    )
    parser.add_argument(
        "--max-applies",
        type=int,
        default=None,
        help="Override maximum number of Easy Apply jobs for this run."
    )
    parser.add_argument(
        "--max-harvest",
        type=int,
        default=None,
        help="Override maximum number of recruiter posts to harvest for this run."
    )
    parser.add_argument(
        "--max-connect",
        type=int,
        default=None,
        help="Override maximum number of connection requests to send for this run."
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Force visible Chrome window."
    )
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="Run headed Chrome off-screen to avoid covering workspace."
    )
    parser.add_argument(
        "--add-company",
        nargs="+",
        help="Add one or more target hiring companies to config.json for connection outreach."
    )
    parser.add_argument(
        "--add-agency",
        nargs="+",
        help="Add one or more recruitment agencies to config.json for connection outreach."
    )
    parser.add_argument(
        "--list-targets",
        action="store_true",
        help="List all configured target hiring companies and recruitment agencies."
    )
    args = parser.parse_args()

    # Handle quick config management without launching browser
    if args.add_company or args.add_agency or args.list_targets:
        cfg = email_agent.load_config()
        outreach_cfg = cfg.setdefault("linkedin", {}).setdefault("connection_outreach", {})

        if args.add_company:
            existing_cos = outreach_cfg.setdefault("target_hiring_companies", [])
            added_c = []
            for co in args.add_company:
                co_clean = co.strip()
                if co_clean and co_clean not in existing_cos:
                    existing_cos.append(co_clean)
                    added_c.append(co_clean)
            config_path = os.path.join(os.path.expanduser('~/Documents/JobAutomation'), 'config.json')
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2)
            print(f"✅ Added {len(added_c)} target hiring company(s): {', '.join(added_c)}")

        if args.add_agency:
            existing_ag = outreach_cfg.setdefault("target_recruitment_agencies", [])
            added_a = []
            for ag in args.add_agency:
                ag_clean = ag.strip()
                if ag_clean and ag_clean not in existing_ag:
                    existing_ag.append(ag_clean)
                    added_a.append(ag_clean)
            config_path = os.path.join(os.path.expanduser('~/Documents/JobAutomation'), 'config.json')
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2)
            print(f"✅ Added {len(added_a)} target recruitment agency(s): {', '.join(added_a)}")

        if args.list_targets:
            logs_d = os.path.expanduser(cfg.get('paths', {}).get('logs', '~/Documents/JobAutomation/logs'))
            cos = get_target_hiring_companies(cfg, logs_d, limit=25)
            ags = get_target_recruitment_agencies(cfg, limit=15)
            print("\n=======================================================")
            print("  🎯 TARGET HIRING COMPANIES & RECRUITMENT AGENCIES")
            print("=======================================================")
            print("🏢 Target Hiring Companies (Hiring Managers / HR):")
            for idx, c in enumerate(cos, 1):
                print(f"  {idx}. {c}")
            print("\n🤝 Target Recruitment & Staffing Agencies:")
            for idx, a in enumerate(ags, 1):
                print(f"  {idx}. {a}")
            print("=======================================================\n")
        sys.exit(0)

    if args.offscreen:
        os.environ["JOB_AUTO_OFFSCREEN"] = "1"
    with MacKeepAwake():
        run_linkedin_pipeline(
            mode=args.mode,
            max_applies=args.max_applies,
            max_harvest=args.max_harvest,
            max_connect=args.max_connect,
            force_headed=args.headed
        )
