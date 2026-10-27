#!/usr/bin/env python3
"""
ATS Bridge & Universal External URL Classifier
Intelligently detects, unshortens, and routes external application links to:
  1. Greenhouse ATS Engine (greenhouse_agent.py)
  2. Workday ATS Engine (workday_agent.py)
  3. Google Forms & Microsoft Forms Engine (forms_agent.py)
Supports:
  - Direct Greenhouse URLs (*.greenhouse.io/*, ?gh_jid=...)
  - Direct Workday URLs (*.myworkdayjobs.com/*)
  - Direct Google Forms URLs (forms.gle/*, docs.google.com/forms/*)
  - Direct Microsoft Forms URLs (forms.office.com/*, forms.microsoft.com/*)
  - Shortened URLs (lnkd.in, bit.ly, t.co, etc.) expanded via HTTP HEAD/GET
  - Dropzone ticket generation and direct dispatch
"""

import os
import re
import logging
from urllib.parse import urlparse

import requests

GREENHOUSE_REGEX = re.compile(
    r'https?://(?:[a-zA-Z0-9\-]+\.)*greenhouse\.io/[^\s\'"<>\)]+|https?://[^\s\'"<>\)]+[?&]gh_jid=\d+',
    re.IGNORECASE
)

WORKDAY_REGEX = re.compile(
    r'https?://[^\s\'"<>\)]+\.myworkdayjobs\.com/[^\s\'"<>\)]+',
    re.IGNORECASE
)

GOOGLE_FORMS_REGEX = re.compile(
    r'https?://(?:forms\.gle/[A-Za-z0-9_\-]+|(?:docs\.)?google\.com/forms/[^\s\'"<>\)]+)',
    re.IGNORECASE
)

MS_FORMS_REGEX = re.compile(
    r'https?://(?:forms\.office\.com/[^\s\'"<>\)]+|forms\.microsoft\.com/[^\s\'"<>\)]+|forms\.cloud\.microsoft/[^\s\'"<>\)]+)',
    re.IGNORECASE
)

KNOWN_SHORTENERS = {
    'lnkd.in', 'bit.ly', 't.co', 'tinyurl.com', 'rb.gy', 'goo.gl',
    'ow.ly', 'buff.ly', 'is.gd', 'cutt.ly', 'shorturl.at'
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def clean_url(url: str) -> str:
    """Strip trailing punctuation from extracted URL."""
    if not url:
        return ""
    return url.rstrip('.,;)]\'>"')


def detect_direct_ats_url(text: str):
    """
    Scan text for direct Greenhouse, Workday, Google Forms, or Microsoft Forms job URLs.
    Returns (ats_type, url) or (None, None).
    """
    if not text:
        return None, None

    # Normalize potential OCR spacing artifacts in URLs: e.g. "forms. gle /" -> "forms.gle/"
    norm_text = re.sub(r'forms\s*\.\s*gle\s*/\s*', 'forms.gle/', text, flags=re.IGNORECASE)
    norm_text = re.sub(r'docs\s*\.\s*google\s*\.\s*com\s*/\s*forms\s*/', 'docs.google.com/forms/', norm_text, flags=re.IGNORECASE)
    norm_text = re.sub(r'forms\s*\.\s*office\s*\.\s*com\s*/\s*', 'forms.office.com/', norm_text, flags=re.IGNORECASE)
    norm_text = re.sub(r'forms\s*\.\s*microsoft\s*\.\s*com\s*/\s*', 'forms.microsoft.com/', norm_text, flags=re.IGNORECASE)

    gh_match = GREENHOUSE_REGEX.search(norm_text)
    if gh_match:
        return "greenhouse", clean_url(gh_match.group(0))

    wd_match = WORKDAY_REGEX.search(norm_text)
    if wd_match:
        return "workday", clean_url(wd_match.group(0))

    gf_match = GOOGLE_FORMS_REGEX.search(norm_text)
    if gf_match:
        return "google_forms", clean_url(gf_match.group(0))

    ms_match = MS_FORMS_REGEX.search(norm_text)
    if ms_match:
        return "ms_forms", clean_url(ms_match.group(0))

    return None, None


def unshorten_url(url: str, timeout: float = 3.5) -> str:
    """
    Follow HTTP redirects to uncover destination URL behind shorteners or tracking redirects.
    """
    clean_u = clean_url(url)
    if not clean_u:
        return ""

    headers = {"User-Agent": USER_AGENT}
    try:
        # Fast HEAD request first
        res = requests.head(clean_u, allow_redirects=True, timeout=timeout, headers=headers)
        if res.url and res.url != clean_u:
            return res.url
    except Exception:
        pass

    try:
        # Fallback to GET with stream=True (does not download body)
        res = requests.get(clean_u, allow_redirects=True, stream=True, timeout=timeout, headers=headers)
        if res.url:
            return res.url
    except Exception:
        pass

    return clean_u


def find_ats_url_in_text(text: str, resolve_shorteners: bool = True):
    """
    Scan text for ATS URLs. If none found directly, checks any shortened links.
    Returns (ats_type, resolved_url) or (None, None).
    """
    if not text:
        return None, None

    # 1. Direct match check (instant)
    ats_type, direct_url = detect_direct_ats_url(text)
    if ats_type:
        return ats_type, direct_url

    if not resolve_shorteners:
        return None, None

    # 2. Find any generic URLs in text
    all_urls = re.findall(r'https?://[^\s\'"<>\)]+', text)
    if not all_urls:
        return None, None

    for raw_u in all_urls:
        u = clean_url(raw_u)
        netloc = urlparse(u).netloc.lower()

        # Prioritize known shorteners, career portals, or application forms
        is_candidate = (
            any(s in netloc for s in KNOWN_SHORTENERS)
            or any(k in u.lower() for k in ('job', 'career', 'apply', 'form', 'hiring', 'recruitment', 'openings'))
        )
        if not is_candidate:
            continue

        resolved = unshorten_url(u, timeout=3.5)
        if not resolved:
            continue

        ats_type, ats_url = detect_direct_ats_url(resolved)
        if ats_type:
            logging.info(f"[ATS Bridge] Unshortened/Redirected '{u}' -> {ats_type.upper()}: {ats_url}")
            return ats_type, ats_url

    return None, None


def queue_ats_job_to_dropzone(ats_type: str, url: str, title: str, company: str,
                              source: str = "Unknown", jd_text: str = "", config: dict = None) -> str:
    """
    Queue an external ATS job ticket into Dropzone so it gets processed automatically.
    Tries Google Drive cloud dropzone first; falls back gracefully to local dropzone.
    """
    config = config or {}
    paths = config.get("paths", {})
    primary_dir = os.path.expanduser(paths.get("dropzone", "~/Documents/JobAutomation/dropzone"))
    local_dir = os.path.expanduser("~/Documents/JobAutomation/dropzone")

    safe_co = re.sub(r'[^\w\-]', '_', (company or "Company"))[:30]
    safe_title = re.sub(r'[^\w\-]', '_', (title or "Role"))[:30]
    filename = f"{source}_{ats_type.upper()}_{safe_co}_{safe_title}.txt"

    content = f"""SOURCE: {source}
ATS_TYPE: {ats_type.upper()}
COMPANY: {company}
ROLE: {title}
APPLY_URL: {url}

JOB DESCRIPTION:
{jd_text}
"""
    # Try primary dropzone (e.g. Google Drive), then local fallback
    for target_dir in [primary_dir, local_dir]:
        try:
            os.makedirs(target_dir, exist_ok=True)
            file_path = os.path.join(target_dir, filename)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            logging.info(f"[ATS Bridge] Created {ats_type.upper()} Dropzone ticket: {filename}")
            return file_path
        except (PermissionError, OSError) as e:
            logging.debug(f"[ATS Bridge] Could not write to {target_dir} ({e}), trying fallback...")
            continue
        except Exception as e:
            logging.warning(f"[ATS Bridge] Unexpected ticket write error: {e}")
            break

    return ""


def dispatch_ats_url(ats_type: str, url: str, config: dict, dry_run: bool = False,
                     force_headed: bool = False, review_mode: bool = False):
    """
    Directly invoke the appropriate ATS engine for a single application URL.
    """
    if ats_type == "greenhouse":
        import greenhouse_agent
        return greenhouse_agent.apply_single_url(
            url, config, dry_run=dry_run, force_headed=force_headed, review_mode=review_mode
        )
    elif ats_type == "workday":
        import workday_agent
        return workday_agent.apply_single_url(
            url, config, dry_run=dry_run, force_headed=force_headed, review_mode=review_mode
        )
    elif ats_type in ("google_forms", "ms_forms", "form", "forms", "google_form", "ms_form"):
        import forms_agent
        return forms_agent.apply_single_url(
            url, config, dry_run=dry_run, force_headed=force_headed, review_mode=review_mode
        )
    else:
        logging.warning(f"[ATS Bridge] Unsupported ATS type: {ats_type}")
        return False

