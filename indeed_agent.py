#!/usr/bin/env python3
"""
Indeed India Daily Job Search & Multi-Step Easy Apply Bot
Integrates with agent.py, config.json, naukri_agent QA bank, and persistent Chrome session.
"""

import os
import sys
import re
import json
import time
import random
import logging
import argparse
from datetime import datetime
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import agent as email_agent
import naukri_agent
import power_utils
from power_utils import MacKeepAwake

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')


def human_delay(min_sec=1.5, max_sec=3.5):
    """Sleep for a randomized human-like interval."""
    time.sleep(random.uniform(min_sec, max_sec))


def notify_macos(title, message, is_urgent=False):
    """Display a non-blocking macOS notification banner respecting quiet_mode."""
    power_utils.notify_macos(title, message, is_urgent=is_urgent)


def load_indeed_ledger(logs_dir):
    """Load persistent ledger of applied Indeed jobs."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = os.path.join(logs_dir, 'indeed_applied_jobs.json')
    default_data = {
        "login_verified": False,
        "last_auto_run_at": None,
        "applied_jobs": {}
    }
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    default_data.update(data)
                    if not isinstance(default_data.get("applied_jobs"), dict):
                        default_data["applied_jobs"] = {}
        except Exception as e:
            logging.warning(f"[Indeed] Could not read indeed_applied_jobs.json: {e}")
    return default_data


def save_indeed_ledger(logs_dir, ledger):
    """Persist Indeed applied jobs ledger."""
    ledger_path = os.path.join(logs_dir, 'indeed_applied_jobs.json')
    try:
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
    except Exception as e:
        logging.warning(f"[Indeed] Could not save indeed_applied_jobs.json: {e}")


def count_applies_today(ledger):
    """Count how many Indeed jobs were successfully applied to today (YYYY-MM-DD)."""
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


def launch_indeed_browser(playwright, config, force_headed=False):
    """Launch a persistent Google Chrome context dedicated to Indeed automation."""
    ind_cfg = config.get('indeed', {})
    profile_dir = os.path.expanduser(
        ind_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/indeed')
    )
    os.makedirs(profile_dir, exist_ok=True)
    naukri_agent.cleanup_stale_chrome_locks(profile_dir)

    chrome_exec = ind_cfg.get('chrome_executable', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    headless = False if force_headed else bool(ind_cfg.get('headless', False))
    if force_headed:
        headless = False

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
    if not force_headed and (os.environ.get("JOB_AUTO_OFFSCREEN") == "1" or ind_cfg.get("offscreen", False)):
        launch_kwargs["args"].append("--window-position=-3000,-3000")

    if os.path.exists(chrome_exec):
        launch_kwargs["executable_path"] = chrome_exec

    context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)
    return context


def is_logged_in_to_indeed(page):
    """Check whether the current Indeed page represents an authenticated session."""
    url = (page.url or "").lower()
    if "secure.indeed.com/auth" in url or "account/login" in url or "signin" in url:
        return False

    # Check for dedicated authenticated subdomain/path
    if "profile.indeed.com" in url or "myjobs.indeed.com" in url:
        if "auth" not in url and "login" not in url:
            return True

    login_form_selectors = [
        "input[type='email']",
        "input#ifl-InputFormField-3",
        "button:has-text('Sign in with Google')"
    ]
    # Check if we are clearly on a dedicated login screen
    if "login" in url or "signin" in url or "auth" in url:
        for sel in login_form_selectors:
            try:
                if page.locator(sel).first.is_visible(timeout=500):
                    return False
            except Exception:
                pass

    logged_in_selectors = [
        "a[data-gnav-element-name='Profile']",
        "button[id*='AccountMenu']",
        "a[href*='profile.indeed.com']",
        "a[href*='myjobs.indeed.com']",
        "button[aria-label*='Account' i]",
        "button[aria-label*='User menu' i]",
        "button[aria-label*='profile' i]",
        "[data-testid='account-menu']",
        "[data-testid='user-avatar']",
        "a[href*='/account/settings']",
        "a[href*='/logout']"
    ]
    for sel in logged_in_selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=800):
                return True
        except Exception:
            pass

    return False


def ensure_logged_in(page, interactive_wait_seconds=300):
    """
    Navigate to Indeed India and verify login.
    If not logged in, wait for the user to log in once in the opened Chrome window.
    """
    logging.info("[Indeed] Navigating to Indeed profile to verify session...")
    try:
        page.goto("https://profile.indeed.com/", wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        logging.warning(f"[Indeed] Initial navigation warning: {e}")

    human_delay(2.5, 4.0)

    # Check if already authenticated on any page/tab
    for p in list(page.context.pages):
        try:
            if is_logged_in_to_indeed(p):
                logging.info("[Indeed] Active Indeed session confirmed!")
                return True
        except Exception:
            pass

    print("\n" + "=" * 65)
    print("  [ACTION REQUIRED] PLEASE LOG IN TO INDEED IN CHROME")
    print("  Google Chrome has opened with the Indeed login page.")
    print("  Please log in using your Indeed account credentials (or Google SSO).")
    print(f"  Waiting up to {interactive_wait_seconds} seconds for login to complete...")
    print("=" * 65 + "\n")
    notify_macos("Indeed Automation", "Please log in once in the opened Chrome window.", is_urgent=True)

    try:
        if "secure.indeed.com" not in (page.url or ""):
            page.goto("https://secure.indeed.com/account/login", wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    try:
        page.bring_to_front()
    except Exception:
        pass

    start_time = time.time()
    while time.time() - start_time < interactive_wait_seconds:
        time.sleep(2)
        try:
            for p in list(page.context.pages):
                if is_logged_in_to_indeed(p):
                    logging.info("[Indeed] Login detected! Saving persistent session.")
                    print("[SUCCESS] Indeed login detected and saved to persistent Chrome profile!")
                    human_delay(2.0, 3.5)
                    return True
        except Exception:
            pass

    logging.error("[Indeed] Timed out waiting for Indeed login.")
    return False


def is_job_suitable(title, snippet, config):
    """Check if the role matches Amrit's tech stack and does not contain excluded keywords."""
    ind_cfg = config.get("indeed", {})
    search_cfg = ind_cfg.get("search", {})
    excluded = search_cfg.get("excluded_title_keywords", [
        ".net", "c#", "php", "wordpress", "salesforce", "sap", "manual testing", "qa automation", "sdet", "director", "vp of"
    ])
    combined_text = f"{title} {snippet}".lower()
    for ex in excluded:
        if ex.lower() in combined_text:
            return False, f"Contains excluded keyword '{ex}'"

    # Check for excessive experience requirement (> 5 years)
    exp_matches = re.findall(r'(\d+)\+?\s*(?:to|-)\s*(\d+)?\s*(?:years|yrs|yo?e)', combined_text)
    for m in exp_matches:
        try:
            min_exp = int(m[0])
            if min_exp > search_cfg.get("max_min_experience_allowed", 5):
                return False, f"Requires {min_exp}+ YOE (exceeds cap)"
        except Exception:
            pass

    return True, "Role is suitable"


def bridge_hr_emails_to_dropzone(jd_text, title, company, job_id, config):
    """Extract HR emails from JD text and create a ticket in Dropzone for agent.py."""
    ind_cfg = config.get("indeed", {})
    if not ind_cfg.get("search", {}).get("extract_hr_emails_to_dropzone", True):
        return []

    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', jd_text or "")
    valid_emails = [e for e in emails if not any(x in e.lower() for x in ("support@", "info@", "help@", "indeed.com", "indeedemail.com"))]
    if not valid_emails:
        # Check if JD contains an external Greenhouse or Workday ATS link
        import ats_bridge
        ats_type, ats_url = ats_bridge.find_ats_url_in_text(jd_text, resolve_shorteners=True)
        if ats_type and ats_url:
            ats_bridge.queue_ats_job_to_dropzone(
                ats_type, ats_url, title, company, source="Indeed_JD", jd_text=jd_text, config=config
            )
            return [f"{ats_type.upper()}:{ats_url}"]
        return []

    dropzone_dir = email_agent.get_dropzone_path(config)
    os.makedirs(dropzone_dir, exist_ok=True)
    filename = f"indeed_{re.sub(r'[^a-zA-Z0-9_]', '_', company)}_{job_id}.txt"
    file_path = os.path.join(dropzone_dir, filename)

    content = f"""SOURCE: Indeed
COMPANY: {company}
ROLE: {title}
JOB_ID: {job_id}
CONTACT_EMAILS: {', '.join(valid_emails)}

JOB DESCRIPTION:
{jd_text}
"""
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        logging.info(f"[Indeed -> Email Bridge] Dropped HR email ticket: {filename} ({valid_emails})")
    except Exception as e:
        logging.warning(f"[Indeed -> Email Bridge] Failed to write dropzone file: {e}")

    return valid_emails


def solve_indeed_application_modal(apply_page, config):
    """
    Solve Indeed's SmartApply application flow and multi-step Easy Apply dialog.
    Fills contact info, selects resume, resolves QA bank questions, and advances to submission.
    """
    qa = config.get("naukri", {}).get("qa_bank", {})
    profile = config.get("profile", {})
    max_steps = 10
    step = 0

    target = apply_page

    # 1. Dismiss cookie banners if present
    try:
        cookie_btn = target.locator("button:has-text('Accept All Cookies'), button:has-text('Accept')").first
        if cookie_btn.count() > 0 and cookie_btn.is_visible(timeout=2000):
            cookie_btn.click(timeout=2000)
            human_delay(1.0, 1.5)
    except Exception:
        pass

    # Detect if on SmartApply subdomain
    try:
        is_smartapply = "smartapply.indeed.com" in (target.url or "")
    except Exception:
        is_smartapply = False

    while step < max_steps:
        step += 1
        human_delay(1.5, 2.5)

        # Check for SmartApply submit button (wait up to 12s if on review-module)
        submit_btn = target.locator(
            "button[data-testid='submit-application-button'], button:has-text('Submit your application'), "
            "button:has-text('Submit application'), button:has-text('Submit'), button:has-text('Apply now'), "
            "button[data-testid='submit-application'], button[data-testid='submit-button']"
        ).first

        if is_smartapply and submit_btn.count() == 0:
            try:
                target.wait_for_selector("button[data-testid='submit-application-button']", timeout=12000)
                submit_btn = target.locator("button[data-testid='submit-application-button']").first
            except Exception:
                pass

        if submit_btn.count() > 0 and submit_btn.is_visible(timeout=2000):
            logging.info("[Indeed SmartApply] Found submit button! Triggering application submission...")
            # Trigger via React Fiber onClick or standard click
            submitted = target.evaluate("""() => {
                const btn = document.querySelector("button[data-testid='submit-application-button'], button[type='submit']");
                if (!btn) return false;
                const key = Object.keys(btn).find(k => k.startsWith('__reactFiber'));
                let fiber = key ? btn[key] : null;
                while (fiber) {
                    if (fiber.memoizedProps && typeof fiber.memoizedProps.onClick === 'function') {
                        try {
                            fiber.memoizedProps.onClick({ preventDefault: () => {}, stopPropagation: () => {} });
                            return true;
                        } catch (e) {}
                    }
                    fiber = fiber.return;
                }
                btn.removeAttribute('disabled');
                btn.disabled = false;
                btn.click();
                return true;
            }""")
            human_delay(3.0, 5.0)

            # Verify submission URL or confirmation
            curr_url = (target.url or "").lower()
            if "post-apply" in curr_url or "confirmation" in curr_url:
                logging.info("[Indeed] Application confirmed submitted (URL: post-apply)!")
                return True

            success_elem = target.locator(
                "h1:has-text('Your application has been submitted'), h1:has-text('Application submitted'), "
                "div:has-text('Your application has been submitted'), span:has-text('Applied')"
            ).first
            if success_elem.count() > 0 and success_elem.is_visible(timeout=3000):
                logging.info("[Indeed] Application confirmed submitted (Success text visible)!")
                return True

            if submitted:
                logging.info("[Indeed] Invoked submit handler on SmartApply!")
                return True

        # 2. Check for Resume selection step
        resume_radio = target.locator(
            "input[type='radio'][name*='resume' i], div[data-testid*='resume'] input[type='radio'], "
            "label:has-text('Resume'), label:has-text('AMRIT')"
        ).first
        if resume_radio.count() > 0 and resume_radio.is_visible(timeout=600):
            try:
                resume_radio.check(timeout=1500)
            except Exception:
                try:
                    resume_radio.click(timeout=1500)
                except Exception:
                    pass

        # 3. Handle Contact Info inputs (Phone, Name, Email) if empty
        phone_input = target.locator("input[type='tel'], input[name*='phone' i], input#input-phoneNumber").first
        if phone_input.count() > 0 and phone_input.is_visible(timeout=800):
            val = (phone_input.input_value() or "").strip()
            if not val:
                phone_input.fill(profile.get("phone", "+91-9876543210"), timeout=2000)

        # 4. Answer screening questions if present
        question_containers = target.locator("fieldset, div.ia-Question, div[class*='Questions']").all()
        for qc in question_containers:
            try:
                q_text = (qc.inner_text(timeout=500) or "").strip().lower()
                if not q_text:
                    continue

                num_input = qc.locator("input[type='text'], input[type='number']").first
                if num_input.count() > 0 and num_input.is_visible(timeout=400):
                    val = (num_input.input_value() or "").strip()
                    if not val:
                        ans = "3"
                        if any(k in q_text for k in ("ctc", "salary", "package")):
                            ans = qa.get("current_ctc_lpa", "12") if "current" in q_text else qa.get("expected_ctc_lpa", "15")
                        elif any(k in q_text for k in ("notice", "days", "joining")):
                            ans = qa.get("notice_period_days", "7")
                        elif any(k in q_text for k in ("experience", "years", "how long")):
                            ans = qa.get("relevant_experience_years", "3")
                        num_input.fill(ans, timeout=1500)

                radio_yes = qc.locator("input[type='radio'][value*='yes' i], label:has-text('Yes')").first
                if radio_yes.count() > 0 and radio_yes.is_visible(timeout=400):
                    if not any(k in q_text for k in ("sponsorship", "visa required", "criminal")):
                        radio_yes.click(timeout=1500)
                    else:
                        radio_no = qc.locator("input[type='radio'][value*='no' i], label:has-text('No')").first
                        if radio_no.count() > 0 and radio_no.is_visible(timeout=400):
                            radio_no.click(timeout=1500)

                select_elem = qc.locator("select").first
                if select_elem.count() > 0 and select_elem.is_visible(timeout=400):
                    try:
                        options = select_elem.locator("option").all()
                        selected = False
                        for opt in options:
                            opt_text = (opt.inner_text() or "").strip().lower()
                            if any(k in opt_text for k in ("yes", "authorized", "immediate", "serving", "3")):
                                val = opt.get_attribute("value")
                                select_elem.select_option(value=val)
                                selected = True
                                break
                        if not selected and len(options) > 1:
                            val = options[1].get_attribute("value")
                            select_elem.select_option(value=val)
                    except Exception:
                        pass
            except Exception:
                pass

        # 5. Click 'Continue' or 'Review your application'
        continue_btn = target.locator(
            "button:has-text('Continue'), button:has-text('Next'), "
            "button:has-text('Review your application'), button:has-text('Review'), "
            "button[data-testid='continue-button'], button.ia-continueButton"
        ).first
        if continue_btn.count() > 0 and continue_btn.is_visible(timeout=1500):
            continue_btn.click(timeout=4000)
            human_delay(1.5, 2.5)
        else:
            break

    # Final check if submitted
    curr_url = (target.url or "").lower()
    if "post-apply" in curr_url or "confirmation" in curr_url:
        return True

    success_indicator = target.locator(
        "h1:has-text('Your application has been submitted'), div:has-text('Application submitted'), "
        "h1:has-text('Applied'), span:has-text('Applied'), [data-testid*='success']"
    ).first
    if success_indicator.count() > 0 and success_indicator.is_visible(timeout=2000):
        return True

    return False


def apply_to_indeed_jobs(page, config, ledger, logs_dir, max_applies=None):
    """
    Search Indeed India across target queries and execute 1-Click / Multi-Step Easy Apply.
    """
    ind_cfg = config.get("indeed", {})
    search_cfg = ind_cfg.get("search", {})
    queries = search_cfg.get("queries", ["Java Full Stack Developer", "Spring Boot Microservices Developer", "Java Backend Developer"])
    locations = search_cfg.get("locations", ["India", "Bengaluru, Karnataka", "Hyderabad, Telangana", "Remote"])
    daily_limit = ind_cfg.get("daily_apply_limit", 50)
    per_run_limit = ind_cfg.get("apply_limit_per_run", 25)

    applied_today = count_applies_today(ledger)
    remaining_daily = max(0, daily_limit - applied_today)
    if remaining_daily <= 0:
        logging.info(f"[Indeed] Daily apply limit reached ({applied_today}/{daily_limit}).")
        print(f"[Indeed] Daily apply limit reached ({applied_today}/{daily_limit}).")
        return 0

    target_applies = min(remaining_daily, per_run_limit)
    if max_applies is not None:
        target_applies = min(target_applies, int(max_applies))

    logging.info(f"[Indeed] Target applies for this run: {target_applies} (Today so far: {applied_today}/{daily_limit})")

    applied_count = 0
    applied_records = ledger.get("applied_jobs", {})

    for query in queries:
        if applied_count >= target_applies:
            break

        for loc in locations[:2]:  # Focus top 2 locations per query to maintain high relevance
            if applied_count >= target_applies:
                break

            encoded_q = quote_plus(query)
            encoded_loc = quote_plus(loc)
            search_url = f"https://in.indeed.com/jobs?q={encoded_q}&l={encoded_loc}&fromage=7"

            logging.info(f"[Indeed] Searching: '{query}' in '{loc}' -> {search_url}")
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                human_delay(2.5, 4.0)
            except Exception as e:
                logging.warning(f"[Indeed] Search navigation warning: {e}")
                continue

            # Dismiss any popup dialogs/overlays (e.g. ifl-portal)
            try:
                page.keyboard.press("Escape")
                close_btn = page.locator("button[aria-label*='close' i], ifl-portal button").first
                if close_btn.count() > 0 and close_btn.is_visible():
                    close_btn.click(timeout=1000)
            except Exception:
                pass

            # Wait for job cards to render
            try:
                page.wait_for_selector("div.cardOutline, div.job_seen_beacon", timeout=12000)
            except Exception:
                pass

            cards = page.locator("div.cardOutline")
            if cards.count() == 0:
                cards = page.locator("div.job_seen_beacon")
            card_count = cards.count()
            logging.info(f"[Indeed] Found {card_count} job cards for '{query}' in '{loc}'")

            for idx in range(card_count):
                if applied_count >= target_applies:
                    break

                try:
                    card = cards.nth(idx)
                    card.scroll_into_view_if_needed(timeout=2000)
                    card_text = (card.inner_text(timeout=2000) or "")

                    # Extract basic info
                    title_elem = card.locator("a[data-jk], h2.jobTitle a, h2.jobTitle").first
                    title = (title_elem.inner_text(timeout=1000) or "").strip()
                    company_elem = card.locator("span[data-testid='company-name'], span.css-63koeb").first
                    company = (company_elem.inner_text(timeout=1000) or "").strip() if company_elem.count() > 0 else "Tech Company"

                    job_key = card.get_attribute("data-jk") or f"ind_{abs(hash(company + title)) % 10000000}"
                    if job_key in applied_records and applied_records[job_key].get("status") == "APPLIED":
                        continue

                    # Filter suitability
                    suitable, reason = is_job_suitable(title, card_text, config)
                    if not suitable:
                        continue

                    # Dismiss any overlay before clicking card
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass

                    # Click the card to open JD pane with force=True
                    try:
                        clickable = title_elem if (title_elem.count() > 0 and title_elem.is_visible(timeout=1000)) else card
                        clickable.click(timeout=3000, force=True)
                    except Exception:
                        card.click(timeout=3000, force=True)
                    human_delay(2.0, 3.5)

                    # Extract JD text from right pane
                    pane = page.locator("#jobsearch-ViewjobPaneWrapper, div.jobsearch-RightPane, div[class*='jobsearch-RightPane']").first
                    jd_text = (pane.inner_text(timeout=3000) or "") if pane.count() > 0 else card_text

                    # Bridge any HR email to Dropzone
                    bridge_hr_emails_to_dropzone(jd_text, title, company, job_key, config)

                    # Look for Apply button scoped to JD pane
                    apply_btn = None
                    if pane.count() > 0:
                        btn_candidates = pane.locator(
                            "a:has-text('Apply now'), button:has-text('Apply now'), a[href*='smartapply.indeed.com'], "
                            "a:has-text('Easily apply'), button:has-text('Easily apply'), "
                            "a:has-text('Apply on company site'), button:has-text('Apply on company site'), "
                            "[id*='indeedApplyButton'], [id*='applyButton']"
                        )
                        for b_i in range(btn_candidates.count()):
                            cand = btn_candidates.nth(b_i)
                            aria = (cand.get_attribute("aria-label") or "").lower()
                            if "filter" in aria:
                                continue
                            if cand.is_visible(timeout=1000):
                                apply_btn = cand
                                break

                    if not apply_btn or not apply_btn.is_visible(timeout=1000):
                        continue

                    # Check if external company site redirect leads to Greenhouse or Workday
                    apply_btn_text = (apply_btn.inner_text(timeout=500) or "").lower()
                    if "company site" in apply_btn_text:
                        import ats_bridge
                        ext_url = (apply_btn.get_attribute("href") or "").strip()
                        if not ext_url:
                            try:
                                parent_a = apply_btn.locator("xpath=..").first
                                if parent_a.count() > 0 and parent_a.get_attribute("href"):
                                    ext_url = parent_a.get_attribute("href").strip()
                            except Exception:
                                pass

                        ats_type, ats_url = None, None
                        if ext_url:
                            ats_type, ats_url = ats_bridge.find_ats_url_in_text(ext_url, resolve_shorteners=True)
                        if not ats_type and jd_text:
                            ats_type, ats_url = ats_bridge.find_ats_url_in_text(jd_text, resolve_shorteners=True)

                        if ats_type and ats_url:
                            logging.info(f"[Indeed] 🎯 External company site leads to {ats_type.upper()}: {ats_url}. Queuing to Dropzone!")
                            ats_bridge.queue_ats_job_to_dropzone(
                                ats_type, ats_url, title, company, source="Indeed_Company_Site", jd_text=jd_text, config=config
                            )
                            applied_records[job_key] = {
                                "company": company,
                                "title": title,
                                "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "status": f"QUEUED_{ats_type.upper()}_ATS",
                                "channel": "Indeed",
                                "ats_url": ats_url
                            }
                            save_indeed_ledger(logs_dir, ledger)
                            continue

                        if ind_cfg.get("only_easily_apply", True):
                            continue

                    logging.info(f"[Indeed] Opening Easy Apply for: '{title}' at '{company}'...")
                    apply_target = page
                    try:
                        with page.expect_popup(timeout=4000) as popup_info:
                            apply_btn.click(timeout=3000, force=True)
                        apply_target = popup_info.value
                        logging.info("[Indeed] Opened SmartApply in new tab.")
                    except Exception:
                        apply_target = page
                        human_delay(1.5, 2.5)

                    submitted = solve_indeed_application_modal(apply_target, config)
                    if submitted:
                        applied_records[job_key] = {
                            "company": company,
                            "title": title,
                            "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "status": "APPLIED",
                            "channel": "Indeed"
                        }
                        applied_count += 1
                        save_indeed_ledger(logs_dir, ledger)
                        logging.info(f"[Indeed] Successfully applied to '{title}' at '{company}' ({applied_count}/{target_applies})")
                        print(f"[Indeed] Successfully applied: '{title}' at '{company}'")

                    if apply_target and apply_target != page:
                        try:
                            apply_target.close()
                        except Exception:
                            pass

                    human_delay(2.5, 4.5)
                except Exception as e:
                    logging.warning(f"[Indeed] Error processing card #{idx}: {e}")
                    continue

    ledger["last_auto_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_indeed_ledger(logs_dir, ledger)
    notify_macos("Indeed Auto-Applier", f"Finished run: Applied to {applied_count} jobs today.")
    return applied_count


def run_indeed_pipeline(mode="apply", max_applies=None, force_headed=False):
    """Entry point for Indeed automation pipeline."""
    config = email_agent.load_config()
    ind_cfg = config.get("indeed", {})
    if not ind_cfg.get("enabled", True):
        logging.info("[Indeed] Indeed agent is disabled in config.json.")
        return

    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    ledger = load_indeed_ledger(logs_dir)

    needs_initial_login = not ledger.get("login_verified", False)
    is_headed = force_headed or (mode == "login") or needs_initial_login

    try:
        with sync_playwright() as playwright:
            context = launch_indeed_browser(playwright, config, force_headed=is_headed)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.bring_to_front()
            except Exception:
                pass

            try:
                wait_secs = 300 if (mode == "login" or needs_initial_login) else 30
                logged_in = ensure_logged_in(page, interactive_wait_seconds=wait_secs)
                if not logged_in:
                    logging.error("[Indeed] Authentication required. Run with --login-indeed to authenticate.")
                    print("[Indeed] Authentication required. Please run: python3 run_all.py --login-indeed")
                    return

                ledger["login_verified"] = True
                save_indeed_ledger(logs_dir, ledger)

                if mode == "login":
                    logging.info("[Indeed] Login verification complete! Persistent profile is ready.")
                    print("\n[SUCCESS] Indeed session verified and persistent Chrome profile is saved!\n")
                    return

                if mode in ("apply", "all"):
                    apply_to_indeed_jobs(page, config, ledger, logs_dir, max_applies=max_applies)

            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except KeyboardInterrupt:
        print("\n[Indeed] Execution cancelled by user.")
        return


def main():
    parser = argparse.ArgumentParser(description="Indeed Automation Agent")
    parser.add_argument("--mode", choices=["login", "apply", "all"], default="apply", help="Operating mode.")
    parser.add_argument("--max-applies", type=int, default=None, help="Maximum jobs to apply in this session.")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser window.")
    parser.add_argument("--headless", action="store_true", help="Force headless browser execution.")
    args = parser.parse_args()

    force_headed = False
    if args.headed or args.mode == "login":
        force_headed = True
    elif args.headless:
        force_headed = False

    with MacKeepAwake():
        run_indeed_pipeline(mode=args.mode, max_applies=args.max_applies, force_headed=force_headed)


if __name__ == "__main__":
    main()
