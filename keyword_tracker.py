#!/usr/bin/env python3
"""
Keyword Performance & Yield Tracker
Tracks keyword queries across all 10 job search portals & ATS systems.
Measures:
  • Jobs scanned per keyword/query
  • Jobs matching candidate criteria (skills/YOE/locations)
  • Applications submitted per keyword
  • Yield / Conversion rate
  • Ingests discovered keywords from recruiter posts & market trends
"""

import os
import json
import logging
from datetime import datetime, timedelta

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
PERF_FILE = os.path.join(LOGS_DIR, 'keyword_performance.json')
DISCOVERED_FILE = os.path.join(LOGS_DIR, 'discovered_keywords.json')
LI_STATS_FILE = os.path.join(LOGS_DIR, 'linkedin_query_stats.json')


def load_keyword_performance():
    """Load persistent keyword performance ledger."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    if not os.path.exists(PERF_FILE):
        return {
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "history": [],
            "aggregate_by_query": {}
        }
    try:
        with open(PERF_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to read keyword performance: {e}")
        return {
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "history": [],
            "aggregate_by_query": {}
        }


def save_keyword_performance(data):
    """Save persistent keyword performance ledger atomically."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp_path = f"{PERF_FILE}.tmp"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, PERF_FILE)
    except Exception as e:
        logging.error(f"Failed to save keyword performance: {e}")


def record_query_run(platform, query, scanned=0, matched=0, applied=0, skipped=0):
    """
    Record metrics for a specific search query execution.
    """
    if not query:
        return
    query_clean = query.strip()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today_str = datetime.now().strftime("%Y-%m-%d")

    data = load_keyword_performance()

    entry = {
        "timestamp": now_str,
        "date": today_str,
        "platform": platform,
        "query": query_clean,
        "scanned": int(scanned),
        "matched": int(matched),
        "applied": int(applied),
        "skipped": int(skipped)
    }
    data.setdefault("history", []).append(entry)

    # Keep last 500 history entries
    if len(data["history"]) > 500:
        data["history"] = data["history"][-500:]

    # Update aggregate
    aggr = data.setdefault("aggregate_by_query", {})
    if query_clean not in aggr:
        aggr[query_clean] = {
            "total_scanned": 0,
            "total_matched": 0,
            "total_applied": 0,
            "platforms": [],
            "last_used": today_str
        }

    q_stat = aggr[query_clean]
    q_stat["total_scanned"] += int(scanned)
    q_stat["total_matched"] += int(matched)
    q_stat["total_applied"] += int(applied)
    if platform not in q_stat["platforms"]:
        q_stat["platforms"].append(platform)
    q_stat["last_used"] = today_str

    save_keyword_performance(data)


def get_daily_keyword_summary(target_date=None):
    """
    Generate an analytics summary for queries run on target_date (default: today).
    """
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")

    data = load_keyword_performance()
    history = data.get("history", [])

    today_entries = [e for e in history if e.get("date") == target_date]

    summary = {
        "date": target_date,
        "total_queries_run": len(today_entries),
        "total_scanned": sum(e.get("scanned", 0) for e in today_entries),
        "total_matched": sum(e.get("matched", 0) for e in today_entries),
        "total_applied": sum(e.get("applied", 0) for e in today_entries),
        "by_query": {},
        "by_platform": {},
        "discovered_trending": []
    }

    for e in today_entries:
        q = e.get("query", "Unknown")
        plt = e.get("platform", "Unknown")

        if q not in summary["by_query"]:
            summary["by_query"][q] = {
                "platforms": set(),
                "scanned": 0,
                "matched": 0,
                "applied": 0
            }
        summary["by_query"][q]["platforms"].add(plt)
        summary["by_query"][q]["scanned"] += e.get("scanned", 0)
        summary["by_query"][q]["matched"] += e.get("matched", 0)
        summary["by_query"][q]["applied"] += e.get("applied", 0)

        if plt not in summary["by_platform"]:
            summary["by_platform"][plt] = {"scanned": 0, "matched": 0, "applied": 0}
        summary["by_platform"][plt]["scanned"] += e.get("scanned", 0)
        summary["by_platform"][plt]["matched"] += e.get("matched", 0)
        summary["by_platform"][plt]["applied"] += e.get("applied", 0)

    # Convert sets to lists
    for q_data in summary["by_query"].values():
        q_data["platforms"] = list(q_data["platforms"])
        # Calculate yield
        scanned = q_data["scanned"]
        q_data["yield_pct"] = round((q_data["applied"] / scanned * 100), 1) if scanned > 0 else 0.0

    # Read discovered keywords from intent miner
    if os.path.exists(DISCOVERED_FILE):
        try:
            with open(DISCOVERED_FILE, 'r', encoding='utf-8') as f:
                disc = json.load(f)
                summary["discovered_trending"] = disc.get("keywords_flat", [])[:10]
        except Exception:
            pass

    return summary


def format_keyword_report_markdown(target_date=None):
    """
    Format a clean Markdown / Telegram report of keyword yield and efficiency.
    """
    summary = get_daily_keyword_summary(target_date)
    dt_str = summary["date"]

    lines = [
        f"🎯 *KEYWORD EFFICIENCY & SEARCH REPORT — {dt_str}*",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    total_scanned = summary["total_scanned"]
    total_applied = summary["total_applied"]
    overall_yield = round((total_applied / total_scanned * 100), 1) if total_scanned > 0 else 0.0

    lines.append(f"🔍 *Total Jobs Scanned:* `{total_scanned}`")
    lines.append(f"✅ *Qualified & Applied:* `{total_applied}` (Conversion: `{overall_yield}%`)")
    lines.append("")

    if summary["by_query"]:
        lines.append("🏆 *Query Performance Breakdown:*")
        # Sort queries by applied count descending
        sorted_queries = sorted(
            summary["by_query"].items(),
            key=lambda x: (x[1]["applied"], x[1]["matched"]),
            reverse=True
        )

        for q, stats in sorted_queries:
            plts = ", ".join(stats["platforms"])
            applied = stats["applied"]
            matched = stats["matched"]
            scanned = stats["scanned"]
            yield_pct = stats.get("yield_pct", 0.0)
            medal = "🔥" if applied >= 5 else ("✨" if applied > 0 else "▫️")
            lines.append(
                f"{medal} *\"{q}\"*\n"
                f"   • Applied: `{applied}` / `{matched}` matched (Scanned: `{scanned}`, Yield: `{yield_pct}%`)\n"
                f"   • Channels: _{plts}_"
            )
        lines.append("")
    else:
        lines.append("ℹ️ *No individual search queries logged for this date yet.*\n")

    if summary["discovered_trending"]:
        trending_str = ", ".join(summary["discovered_trending"][:8])
        lines.append("💡 *Trending Keywords Mined from Recruiters:*")
        lines.append(f"_{trending_str}_")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_keyword_report_markdown())
