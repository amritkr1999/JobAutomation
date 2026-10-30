#!/usr/bin/env python3
"""
Unified Quota & Wave Automation Manager
Calculates real-time daily application progress across all 12 pipelines,
tracks remaining quotas against configured caps, manages 5-wave daily pacing,
and formats visual progress bars for CLI and Telegram Bot.
"""

import os
import sys
import json
import math
from datetime import datetime, time as dtime

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')

# 5 Scheduled Daily Waves (IST)
SCHEDULED_WAVES = [
    {"wave": 1, "time_str": "09:30 AM", "hour": 9, "minute": 30},
    {"wave": 2, "time_str": "11:30 AM", "hour": 11, "minute": 30},
    {"wave": 3, "time_str": "01:30 PM", "hour": 13, "minute": 30},
    {"wave": 4, "time_str": "03:30 PM", "hour": 15, "minute": 30},
    {"wave": 5, "time_str": "05:30 PM", "hour": 17, "minute": 30},
]


def load_config():
    """Load system configuration safely."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _count_json_records(filename, obj_key="applied_jobs", status_keys=("APPLIED", "SUBMITTED"), today_str=None):
    """Generic counter for jobs/records applied today in logs JSON."""
    if today_str is None:
        today_str = datetime.now().strftime("%Y-%m-%d")

    path = os.path.join(LOGS_DIR, filename)
    if not os.path.exists(path):
        return 0, 0

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            records = data.get(obj_key, {})
            if isinstance(records, list):
                total = len(records)
                today_count = sum(
                    1 for v in records
                    if str(v.get("applied_at", v.get("sent_at", ""))).startswith(today_str)
                    and (not status_keys or v.get("status") in status_keys)
                )
                return today_count, total
            elif isinstance(records, dict):
                total = len(records)
                today_count = sum(
                    1 for v in records.values()
                    if isinstance(v, dict)
                    and str(v.get("applied_at", v.get("sent_at", ""))).startswith(today_str)
                    and (not status_keys or v.get("status") in status_keys)
                )
                return today_count, total
    except Exception:
        pass
    return 0, 0


def get_current_wave_info():
    """
    Determine current wave number, next scheduled wave, and remaining waves for today.
    """
    now = datetime.now()
    now_time = now.time()

    current_wave = None
    next_wave = None
    remaining_waves = []

    for w in SCHEDULED_WAVES:
        w_time = dtime(hour=w["hour"], minute=w["minute"])
        if now_time >= w_time:
            current_wave = w
        else:
            if next_wave is None:
                next_wave = w
            remaining_waves.append(w)

    current_wave_num = current_wave["wave"] if current_wave else 0
    next_wave_str = next_wave["time_str"] if next_wave else "Tomorrow 09:30 AM"

    return {
        "current_wave_num": current_wave_num,
        "current_wave": current_wave,
        "next_wave": next_wave,
        "next_wave_str": next_wave_str,
        "remaining_waves_count": len(remaining_waves) if remaining_waves else 1,
        "total_waves": len(SCHEDULED_WAVES)
    }


def get_all_quotas(today_str=None):
    """
    Compute live status, daily limits, submitted counts, and remaining quotas
    for all 12 pipelines.
    """
    if today_str is None:
        today_str = datetime.now().strftime("%Y-%m-%d")

    config = load_config()

    # 1. Pipeline Definitions & Daily Limits
    pipelines = [
        # ATS & Enterprise Portals (On-Demand Downstream Handlers)
        {
            "key": "greenhouse",
            "name": "Greenhouse ATS",
            "short_name": "Greenhouse",
            "category": "Downstream ATS (On-Demand)",
            "icon": "🏢",
            "file": "greenhouse_applied_jobs.json",
            "obj_key": "applied_jobs",
            "status_keys": ("APPLIED", "SUBMITTED"),
            "limit": config.get("greenhouse", {}).get("daily_apply_limit", 25),
            "run_limit": config.get("greenhouse", {}).get("apply_limit_per_run", 10),
            "enabled": config.get("greenhouse", {}).get("enabled", True),
            "is_downstream": True,
        },
        {
            "key": "workday",
            "name": "Workday ATS",
            "short_name": "Workday",
            "category": "Downstream ATS (On-Demand)",
            "icon": "💼",
            "file": "workday_applied_jobs.json",
            "obj_key": "applied_jobs",
            "status_keys": ("APPLIED", "SUBMITTED"),
            "limit": config.get("workday", {}).get("daily_apply_limit", 20),
            "run_limit": config.get("workday", {}).get("apply_limit_per_run", 5),
            "enabled": config.get("workday", {}).get("enabled", True),
            "is_downstream": True,
        },
        {
            "key": "forms",
            "name": "Google & MS Forms",
            "short_name": "Web Forms",
            "category": "Downstream ATS (On-Demand)",
            "icon": "📋",
            "file": "forms_applied.json",
            "obj_key": "applied_forms",
            "status_keys": ("APPLIED", "SUBMITTED"),
            "limit": config.get("forms", {}).get("daily_apply_limit", 25),
            "run_limit": config.get("forms", {}).get("apply_limit_per_run", 10),
            "enabled": config.get("forms", {}).get("enabled", True),
            "is_downstream": True,
        },

        # Top Job Boards
        {
            "key": "naukri",
            "name": "Naukri.com",
            "short_name": "Naukri",
            "category": "Job Boards",
            "icon": "💼",
            "file": "naukri_applied_jobs.json",
            "obj_key": "applied_jobs",
            "status_keys": ("APPLIED",),
            "limit": config.get("naukri", {}).get("daily_apply_limit", 100),
            "run_limit": config.get("naukri", {}).get("apply_limit_per_run", 50),
            "enabled": config.get("naukri", {}).get("enabled", True),
        },
        {
            "key": "instahyre",
            "name": "Instahyre",
            "short_name": "Instahyre",
            "category": "Job Boards",
            "icon": "⚡",
            "file": "instahyre_applied_jobs.json",
            "obj_key": "applied_jobs",
            "status_keys": ("APPLIED",),
            "limit": config.get("instahyre", {}).get("daily_apply_limit", 100),
            "run_limit": config.get("instahyre", {}).get("apply_limit_per_run", 50),
            "enabled": config.get("instahyre", {}).get("enabled", True),
        },
        {
            "key": "linkedin",
            "name": "LinkedIn Easy Apply",
            "short_name": "LinkedIn",
            "category": "Job Boards",
            "icon": "🌐",
            "file": "linkedin_applied_jobs.json",
            "obj_key": "applied_jobs",
            "status_keys": ("APPLIED",),
            "limit": config.get("linkedin", {}).get("daily_apply_limit", 20),
            "run_limit": 10,
            "enabled": config.get("linkedin", {}).get("enabled", True),
        },
        {
            "key": "linkedin_connect",
            "name": "LinkedIn Connections",
            "short_name": "LI Connect",
            "category": "Job Boards",
            "icon": "🤝",
            "file": "linkedin_connections.json",
            "obj_key": "connections",
            "status_keys": ("INVITED",),
            "limit": config.get("linkedin", {}).get("connection_outreach", {}).get("daily_limit", 12),
            "run_limit": 6,
            "enabled": config.get("linkedin", {}).get("connection_outreach", {}).get("enabled", True),
        },
        {
            "key": "indeed",
            "name": "Indeed India",
            "short_name": "Indeed",
            "category": "Job Boards",
            "icon": "🔍",
            "file": "indeed_applied_jobs.json",
            "obj_key": "applied_jobs",
            "status_keys": ("APPLIED",),
            "limit": config.get("indeed", {}).get("daily_apply_limit", 50),
            "run_limit": config.get("indeed", {}).get("apply_limit_per_run", 25),
            "enabled": config.get("indeed", {}).get("enabled", True),
        },
        {
            "key": "shine",
            "name": "Shine.com",
            "short_name": "Shine",
            "category": "Job Boards",
            "icon": "✨",
            "file": "shine_applied_jobs.json",
            "obj_key": "applied_jobs",
            "status_keys": ("APPLIED",),
            "limit": config.get("shine", {}).get("daily_apply_limit", 50),
            "run_limit": config.get("shine", {}).get("apply_limit_per_run", 25),
            "enabled": config.get("shine", {}).get("enabled", True),
        },
        {
            "key": "wellfound",
            "name": "Wellfound (AngelList)",
            "short_name": "Wellfound",
            "category": "Job Boards",
            "icon": "🚀",
            "file": "wellfound_applied_jobs.json",
            "obj_key": "applied_jobs",
            "status_keys": ("APPLIED",),
            "limit": config.get("wellfound", {}).get("daily_apply_limit", 40),
            "run_limit": config.get("wellfound", {}).get("apply_limit_per_run", 20),
            "enabled": config.get("wellfound", {}).get("enabled", True),
        },
        {
            "key": "foundit",
            "name": "Foundit (Monster India)",
            "short_name": "Foundit",
            "category": "Job Boards",
            "icon": "🎯",
            "file": "foundit_applied_jobs.json",
            "obj_key": "applied_jobs",
            "status_keys": ("APPLIED",),
            "limit": config.get("foundit", {}).get("daily_apply_limit", 50),
            "run_limit": config.get("foundit", {}).get("apply_limit_per_run", 25),
            "enabled": config.get("foundit", {}).get("enabled", True),
        },
        {
            "key": "jobgether",
            "name": "Jobgether Remote",
            "short_name": "Jobgether",
            "category": "Job Boards",
            "icon": "🌍",
            "file": "jobgether_applied_jobs.json",
            "obj_key": "applied_jobs",
            "status_keys": ("APPLIED",),
            "limit": config.get("jobgether", {}).get("daily_apply_limit", 50),
            "run_limit": config.get("jobgether", {}).get("apply_limit_per_run", 25),
            "enabled": config.get("jobgether", {}).get("enabled", True),
        },

        # Direct Cold Email Outreach
        {
            "key": "cold_email",
            "name": "Direct Cold Outreach",
            "short_name": "Cold Email",
            "category": "Direct Outreach",
            "icon": "📧",
            "file": "sent_emails.json",
            "obj_key": None,  # Top level dict of sent records
            "status_keys": None,
            "limit": config.get("cold_email", {}).get("daily_send_limit", 25),
            "run_limit": 15,
            "enabled": config.get("cold_email", {}).get("enabled", True),
        }
    ]

    # Calculate actuals
    portal_stats = {}
    total_applied = 0
    total_limit = 0
    total_remaining = 0

    for p in pipelines:
        key = p["key"]
        limit = p["limit"]
        enabled = p["enabled"]

        if key == "cold_email":
            # Cold emails are identified in sent_emails.json where application_type == 'COLD_OUTREACH'
            sent_path = os.path.join(LOGS_DIR, "sent_emails.json")
            applied = 0
            total_history = 0
            if os.path.exists(sent_path):
                try:
                    with open(sent_path, 'r', encoding='utf-8') as f:
                        sdata = json.load(f)
                        total_history = len(sdata)
                        applied = sum(
                            1 for v in sdata.values()
                            if str(v.get("sent_at", "")).startswith(today_str)
                            and v.get("application_type") == "COLD_OUTREACH"
                        )
                except Exception:
                    pass
        else:
            applied, total_history = _count_json_records(
                p["file"], obj_key=p["obj_key"], status_keys=p["status_keys"], today_str=today_str
            )

        is_downstream = p.get("is_downstream", False)
        remaining = 0 if is_downstream else max(0, limit - applied)
        percent = 100.0 if is_downstream else (round((applied / limit * 100.0), 1) if limit > 0 else 100.0)
        completed = True if is_downstream else (applied >= limit)

        p_stat = {
            "key": key,
            "name": p["name"],
            "short_name": p["short_name"],
            "category": p["category"],
            "icon": p["icon"],
            "applied": applied,
            "limit": limit if not is_downstream else applied,
            "run_limit": p["run_limit"],
            "remaining": remaining,
            "percent": percent,
            "completed": completed,
            "enabled": enabled,
            "is_downstream": is_downstream,
            "total_history": total_history
        }

        portal_stats[key] = p_stat

        if enabled:
            total_applied += applied
            if not is_downstream:
                total_limit += limit
                total_remaining += remaining

    overall_percent = round((total_applied / total_limit * 100.0), 1) if total_limit > 0 else 100.0
    all_completed = (total_remaining == 0)

    wave_info = get_current_wave_info()

    return {
        "today_str": today_str,
        "portals": portal_stats,
        "total_applied": total_applied,
        "total_limit": total_limit,
        "total_remaining": total_remaining,
        "overall_percent": overall_percent,
        "all_completed": all_completed,
        "wave_info": wave_info
    }


def get_remaining(portal_key):
    """Return remaining quota for a specific portal today."""
    quotas = get_all_quotas()
    p = quotas["portals"].get(portal_key)
    if not p:
        return 0
    return p["remaining"]


def is_portal_completed(portal_key):
    """Check if a specific portal has already satisfied its daily quota."""
    return get_remaining(portal_key) <= 0


def is_all_quotas_completed():
    """Check if all enabled portals have satisfied their daily quotas."""
    quotas = get_all_quotas()
    return quotas["all_completed"]


def get_wave_slice(portal_key):
    """
    Calculate how many applications should be targeted in the current wave
    for a given portal, distributing the remaining balance across remaining waves.
    """
    quotas = get_all_quotas()
    p = quotas["portals"].get(portal_key)
    if not p or not p["enabled"]:
        return 0

    remaining = p["remaining"]
    if remaining <= 0:
        return 0

    rem_waves = max(1, quotas["wave_info"]["remaining_waves_count"])
    target_slice = math.ceil(remaining / rem_waves)

    # Respect the portal's safety run_limit
    run_limit = p["run_limit"]
    return min(remaining, max(target_slice, run_limit))


def format_progress_bar(percent, width=10):
    """Format a clean visual progress bar: [██████░░░░]"""
    clamped = max(0.0, min(100.0, float(percent)))
    filled_len = int(round(width * clamped / 100.0))
    bar = "█" * filled_len + "░" * (width - filled_len)
    return f"[{bar}]"


def format_quota_progress_markdown():
    """
    Render a comprehensive, visual Telegram-ready markdown report of daily quotas,
    real-time progress bars, and scheduled wave status.
    """
    quotas = get_all_quotas()
    today_dt = datetime.now()
    today_formatted = today_dt.strftime("%d %b %Y")
    now_time_formatted = today_dt.strftime("%I:%M %p")

    wave_info = quotas["wave_info"]
    curr_wave_str = f"Wave {wave_info['current_wave_num']} of {wave_info['total_waves']}" if wave_info['current_wave_num'] > 0 else "Pre-Wave 1"
    next_wave_str = wave_info["next_wave_str"]

    total_applied = quotas["total_applied"]
    total_limit = quotas["total_limit"]
    total_remaining = quotas["total_remaining"]
    overall_pct = quotas["overall_percent"]
    overall_bar = format_progress_bar(overall_pct, width=10)

    portals = quotas["portals"]

    # Group by category
    categories = {
        "Enterprise & ATS Systems": ["greenhouse", "workday", "forms"],
        "Top Job Boards": ["naukri", "instahyre", "linkedin", "linkedin_connect", "indeed", "shine", "wellfound", "foundit", "jobgether"],
        "Direct Cold Email": ["cold_email"]
    }

    lines = []
    lines.append(f"🎯 *DAILY QUOTA & PROGRESS DASHBOARD*")
    lines.append(f"📅 *Date:* `{today_formatted}` | 🕒 *Time:* `{now_time_formatted} IST`")
    lines.append(f"🌊 *Wave:* `{curr_wave_str}` | *Next Wave:* `{next_wave_str}`")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📊 *OVERALL DAILY TARGET:*")
    lines.append(f"`{overall_bar} {overall_pct}%` (`{total_applied} / {total_limit}`)")
    if total_remaining > 0:
        lines.append(f"⏳ *Balance Remaining:* `{total_remaining}` applications/actions left")
    else:
        lines.append(f"🎉 *100% COMPLETE!* All daily application quotas achieved!")
    lines.append("")

    for cat_name, p_keys in categories.items():
        cat_applied = sum(portals[k]["applied"] for k in p_keys if k in portals)
        cat_limit = sum(portals[k]["limit"] for k in p_keys if k in portals)
        lines.append(f"*{cat_name}* (`{cat_applied}/{cat_limit}`):")
        for k in p_keys:
            p = portals.get(k)
            if not p:
                continue
            bar = format_progress_bar(p["percent"], width=8)
            check = "✅ " if p["completed"] else "• "
            rem_str = "Done" if p["completed"] else f"`{p['remaining']}` left"
            lines.append(f"{check}{p['icon']} *{p['short_name']}:* `{bar}` `{p['applied']}/{p['limit']}` ({rem_str})")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 *Commands:*")
    lines.append("• `/run all` - Launch full wave sweep right now")
    lines.append("• `/run [portal]` - Sweep specific portal (e.g. `/run workday`)")
    lines.append("• `/status` - Summary report with keyword yields")

    return "\n".join(lines)


def print_cli_summary():
    """Print clean terminal summary of current quota metrics."""
    quotas = get_all_quotas()
    wave_info = quotas["wave_info"]

    print("\n" + "=" * 65)
    print(f"       DAILY APPLICATION QUOTA & WAVE MONITOR")
    print("=" * 65)
    print(f" • Date:               {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print(f" • Current Wave:       Wave {wave_info['current_wave_num']} of {wave_info['total_waves']}")
    print(f" • Next Auto-Sweep:    {wave_info['next_wave_str']}")
    print(f" • Overall Progress:   {format_progress_bar(quotas['overall_percent'], 12)} {quotas['overall_percent']}%")
    print(f" • Applications Sent:  {quotas['total_applied']} / {quotas['total_limit']} (Remaining: {quotas['total_remaining']})")
    print("-" * 65)

    for k, p in quotas["portals"].items():
        status = "COMPLETED" if p["completed"] else f"{p['remaining']} remaining"
        bar = format_progress_bar(p["percent"], 8)
        print(f" {p['icon']} {p['name']:<25} {bar} {p['applied']:>3}/{p['limit']:<3}  ({status})")

    print("=" * 65 + "\n")


if __name__ == "__main__":
    print_cli_summary()
