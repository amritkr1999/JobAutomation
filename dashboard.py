#!/usr/bin/env python3
"""
JobAutomation Mission Control Dashboard (dashboard.py)
A centralized, zero-dependency, real-time web command center for managing
the autonomous job application system, daily quota pacing, recruiter CRM,
inbound email reply approvals, candidate tasks, and bot health.
"""

import os
import sys
import json
import time
import re
import glob
import logging
import argparse
import webbrowser
import threading
import subprocess
import urllib.parse
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# Core system imports
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import quota_manager
import task_manager
import reply_agent
import crm_tracker
import qa_memory
import pending_qa

CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
GLOBAL_SERVER = None


def load_config() -> dict:
    """Safely load config.json."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# -----------------------------------------------------------------------------
# Background Action Execution Engine
# -----------------------------------------------------------------------------

class ActionManager:
    """Thread-safe runner for background automation tasks triggered from web UI."""

    def __init__(self):
        self._lock = threading.Lock()
        self.is_running = False
        self.current_action = None
        self.started_at = None
        self.completed_at = None
        self.exit_code = None
        self.logs = []
        self._process = None

    def trigger_action(self, action_key: str, params: dict = None) -> tuple[bool, str]:
        """Start a background CLI action."""
        with self._lock:
            if self.is_running:
                return False, f"Another action is already running: {self.current_action}"

            cmd = [sys.executable, os.path.join(BASE_DIR, 'run_all.py')]
            desc = action_key

            if action_key == 'wave':
                cmd.extend(['--apply-all', '--headless', '--quiet'])
                desc = "Run Application Wave"
            elif action_key == 'refresh_all':
                cmd.extend(['--refresh-all'])
                desc = "Refresh All Profiles"
            elif action_key == 'inbound':
                cmd.extend(['--inbound'])
                desc = "Inbound Recruiter Email Sweep"
            elif action_key == 'dropzone':
                cmd.extend(['--emails'])
                desc = "Process Cold Email Dropzone"
            elif action_key == 'optimize':
                cmd.extend(['--optimize'])
                desc = "Self-Learning Optimization Cycle"
            elif action_key.startswith('apply_'):
                portal = action_key.replace('apply_', '')
                cmd.extend([f'--apply-{portal}'])
                desc = f"Apply via {portal.title()}"
            elif action_key.startswith('refresh_'):
                portal = action_key.replace('refresh_', '')
                cmd.extend([f'--refresh-{portal}'])
                desc = f"Refresh {portal.title()} Profile"
            elif action_key.startswith('login_'):
                portal = action_key.replace('login_', '')
                cmd.extend([f'--login-{portal}', '--headed'])
                desc = f"Interactive Login for {portal.title()}"
            elif action_key == 'resume_editor':
                # Launch resume editor web server in background
                subprocess.Popen([sys.executable, os.path.join(BASE_DIR, 'resume_editor.py'), '--web'])
                return True, "Launched Resume Editor at http://127.0.0.1:8765"
            else:
                return False, f"Unknown action: {action_key}"

            self.is_running = True
            self.current_action = desc
            self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.completed_at = None
            self.exit_code = None
            self.logs = [f"[{self.started_at}] Started: {' '.join(cmd)}"]

            t = threading.Thread(target=self._run_process, args=(cmd,), daemon=True)
            t.start()
            return True, f"Triggered {desc} successfully."

    def _run_process(self, cmd: list):
        """Execute subprocess and capture output lines."""
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=BASE_DIR,
                bufsize=1
            )
            for line in iter(self._process.stdout.readline, ''):
                clean_line = line.strip()
                if clean_line:
                    with self._lock:
                        self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {clean_line}")
                        if len(self.logs) > 300:
                            self.logs.pop(0)
            self._process.wait()
            with self._lock:
                self.exit_code = self._process.returncode
        except Exception as e:
            with self._lock:
                self.logs.append(f"ERROR: Execution failed: {e}")
                self.exit_code = -1
        finally:
            with self._lock:
                self.is_running = False
                self.completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def get_status(self) -> dict:
        """Get live status snapshot."""
        with self._lock:
            return {
                "is_running": self.is_running,
                "current_action": self.current_action,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "exit_code": self.exit_code,
                "recent_logs": self.logs[-40:]
            }


ACTION_RUNNER = ActionManager()


# -----------------------------------------------------------------------------
# System State Aggregator
# -----------------------------------------------------------------------------

def get_system_state() -> dict:
    """Compile a complete, real-time snapshot of the entire automation suite."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    config = load_config()

    # 1. Quotas & Wave Pacing
    quotas = quota_manager.get_all_quotas(today_str)
    wave_info = quotas.get("wave_info", {})

    # 2. Candidate Tasks Summary
    tasks_data = task_manager.load_tasks()
    raw_tasks = tasks_data.get("tasks", {})
    pending_tasks = [t for t in raw_tasks.values() if t.get("status") == "PENDING"]
    high_priority_tasks = [t for t in pending_tasks if t.get("priority") == "HIGH"]
    call_tasks = [t for t in pending_tasks if t.get("type") == "SCHEDULE_CALL"]
    link_tasks = [t for t in pending_tasks if t.get("type") == "APPLY_LINK_MANUAL"]
    review_tasks = [t for t in pending_tasks if t.get("type") == "REVIEW_REPLY_DRAFT"]

    # 3. Recruiter Email Drafts Summary
    drafts_data = reply_agent.load_drafts()
    raw_drafts = drafts_data.get("drafts", {})
    pending_drafts = [d for d in raw_drafts.values() if d.get("status") == "PENDING"]

    # 4. Recruiter CRM Pipeline Summary
    crm_summary = crm_tracker.get_summary()
    crm_data = crm_tracker.load_crm()
    crm_threads = crm_data.get("threads", {})

    stage_counts = {
        "OUTREACH_SENT": sum(1 for t in crm_threads.values() if t.get("stage") == "OUTREACH_SENT"),
        "REPLIED": sum(1 for t in crm_threads.values() if t.get("stage") == "REPLIED"),
        "LINK_RECEIVED": sum(1 for t in crm_threads.values() if t.get("stage") == "LINK_RECEIVED"),
        "SCREENING_REQUESTED": sum(1 for t in crm_threads.values() if t.get("stage") == "SCREENING_REQUESTED"),
        "INTERVIEW_SCHEDULED": sum(1 for t in crm_threads.values() if t.get("stage") == "INTERVIEW_SCHEDULED"),
        "OFFER": sum(1 for t in crm_threads.values() if t.get("stage") == "OFFER"),
    }

    # 5. Stuck & Debug Screenshots
    screenshot_files = glob.glob(os.path.join(LOGS_DIR, "*.png"))
    stuck_screenshots = []
    for f in sorted(screenshot_files, key=os.path.getmtime, reverse=True):
        basename = os.path.basename(f)
        if any(k in basename for k in ("stuck", "debug", "dry_run")):
            stuck_screenshots.append({
                "filename": basename,
                "modified_at": datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M:%S"),
                "size_kb": round(os.path.getsize(f) / 1024, 1),
                "is_stuck": "stuck" in basename
            })

    # 6. Portal Details & Session Matrix
    portals_info = []

    def _read_portal_file(filename, job_key="applied_jobs"):
        p = os.path.join(LOGS_DIR, filename)
        if os.path.exists(p):
            try:
                with open(p, 'r', encoding='utf-8') as pf:
                    d = json.load(pf)
                    return d, d.get(job_key, {})
            except Exception:
                pass
        return {}, {}

    # Naukri
    n_data, n_jobs = _read_portal_file("naukri_applied_jobs.json")
    n_today = sum(1 for v in n_jobs.values() if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str))
    portals_info.append({
        "key": "naukri",
        "name": "Naukri.com",
        "icon": "💼",
        "category": "Job Portal",
        "applied_today": n_today,
        "daily_limit": config.get("naukri", {}).get("daily_apply_limit", 100),
        "total_jobs": len(n_jobs),
        "last_refresh": n_data.get("last_profile_refresh_at") or "Never",
        "session_status": "Active",
        "is_healthy": True,
        "supports_refresh": True
    })

    # Instahyre
    i_data, i_jobs = _read_portal_file("instahyre_applied_jobs.json")
    i_today = sum(1 for v in i_jobs.values() if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str))
    i_auth = i_data.get("login_verified", False)
    portals_info.append({
        "key": "instahyre",
        "name": "Instahyre",
        "icon": "⚡",
        "category": "Job Portal",
        "applied_today": i_today,
        "daily_limit": config.get("instahyre", {}).get("daily_apply_limit", 100),
        "total_jobs": len(i_jobs),
        "last_refresh": i_data.get("last_profile_refresh_at") or "Never",
        "session_status": "Active" if i_auth else "Needs Login",
        "is_healthy": i_auth,
        "supports_refresh": True
    })

    # LinkedIn
    li_data, li_jobs = _read_portal_file("linkedin_applied_jobs.json")
    li_today = sum(1 for v in li_jobs.values() if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str))
    li_auth = li_data.get("login_verified", False)
    portals_info.append({
        "key": "linkedin",
        "name": "LinkedIn",
        "icon": "🌐",
        "category": "Professional",
        "applied_today": li_today,
        "daily_limit": config.get("linkedin", {}).get("daily_apply_limit", 20),
        "total_jobs": len(li_jobs),
        "last_refresh": "N/A",
        "session_status": "Active" if li_auth else "Needs Login",
        "is_healthy": li_auth,
        "supports_refresh": False
    })

    # Indeed
    ind_data, ind_jobs = _read_portal_file("indeed_applied_jobs.json")
    ind_today = sum(1 for v in ind_jobs.values() if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str))
    ind_auth = ind_data.get("login_verified", False)
    portals_info.append({
        "key": "indeed",
        "name": "Indeed India",
        "icon": "🔍",
        "category": "Job Portal",
        "applied_today": ind_today,
        "daily_limit": config.get("indeed", {}).get("daily_apply_limit", 50),
        "total_jobs": len(ind_jobs),
        "last_refresh": "N/A",
        "session_status": "Active" if ind_auth else "Needs Login",
        "is_healthy": ind_auth,
        "supports_refresh": False
    })

    # Shine
    sh_data, sh_jobs = _read_portal_file("shine_applied_jobs.json")
    sh_today = sum(1 for v in sh_jobs.values() if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str))
    sh_auth = sh_data.get("login_verified", False)
    portals_info.append({
        "key": "shine",
        "name": "Shine.com",
        "icon": "✨",
        "category": "Job Portal",
        "applied_today": sh_today,
        "daily_limit": config.get("shine", {}).get("daily_apply_limit", 50),
        "total_jobs": len(sh_jobs),
        "last_refresh": sh_data.get("last_profile_refresh_at") or "Never",
        "session_status": "Active" if sh_auth else "Needs Login",
        "is_healthy": sh_auth,
        "supports_refresh": True
    })

    # Wellfound
    wf_data, wf_jobs = _read_portal_file("wellfound_applied_jobs.json")
    wf_today = sum(1 for v in wf_jobs.values() if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str))
    wf_auth = wf_data.get("login_verified", False)
    portals_info.append({
        "key": "wellfound",
        "name": "Wellfound",
        "icon": "🚀",
        "category": "Startups",
        "applied_today": wf_today,
        "daily_limit": config.get("wellfound", {}).get("daily_apply_limit", 40),
        "total_jobs": len(wf_jobs),
        "last_refresh": wf_data.get("last_profile_refresh_at") or "Never",
        "session_status": "Active" if wf_auth else "Needs Login",
        "is_healthy": wf_auth,
        "supports_refresh": True
    })

    # Jobgether
    jg_data, jg_jobs = _read_portal_file("jobgether_applied_jobs.json")
    jg_today = sum(1 for v in jg_jobs.values() if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str))
    jg_auth = jg_data.get("login_verified", False)
    portals_info.append({
        "key": "jobgether",
        "name": "Jobgether",
        "icon": "🌍",
        "category": "Remote Global",
        "applied_today": jg_today,
        "daily_limit": config.get("jobgether", {}).get("daily_apply_limit", 50),
        "total_jobs": len(jg_jobs),
        "last_refresh": jg_data.get("last_profile_refresh_at") or "Never",
        "session_status": "Active" if jg_auth else "Needs Login",
        "is_healthy": jg_auth,
        "supports_refresh": True
    })

    # Foundit
    fnd_data, fnd_jobs = _read_portal_file("foundit_applied_jobs.json")
    fnd_today = sum(1 for v in fnd_jobs.values() if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str))
    fnd_auth = fnd_data.get("login_verified", False)
    portals_info.append({
        "key": "foundit",
        "name": "Foundit",
        "icon": "🎯",
        "category": "Job Portal",
        "applied_today": fnd_today,
        "daily_limit": config.get("foundit", {}).get("daily_apply_limit", 50),
        "total_jobs": len(fnd_jobs),
        "last_refresh": fnd_data.get("last_profile_refresh_at") or "Never",
        "session_status": "Active" if fnd_auth else "Needs Login",
        "is_healthy": fnd_auth,
        "supports_refresh": True
    })

    # Greenhouse ATS
    gh_data, gh_jobs = _read_portal_file("greenhouse_applied_jobs.json")
    gh_today = sum(1 for v in gh_jobs.values() if v.get("status") in ("APPLIED", "SUBMITTED") and str(v.get("applied_at", "")).startswith(today_str))
    portals_info.append({
        "key": "greenhouse",
        "name": "Greenhouse ATS",
        "icon": "🏢",
        "category": "Enterprise ATS",
        "applied_today": gh_today,
        "daily_limit": config.get("greenhouse", {}).get("daily_apply_limit", 25),
        "total_jobs": len(gh_jobs),
        "last_refresh": gh_data.get("last_run_at") or "Never",
        "session_status": "Active (API)",
        "is_healthy": True,
        "supports_refresh": False
    })

    # Workday ATS
    wd_data, wd_jobs = _read_portal_file("workday_applied_jobs.json")
    wd_today = sum(1 for v in wd_jobs.values() if v.get("status") in ("APPLIED", "SUBMITTED") and str(v.get("applied_at", "")).startswith(today_str))
    portals_info.append({
        "key": "workday",
        "name": "Workday ATS",
        "icon": "💻",
        "category": "Enterprise ATS",
        "applied_today": wd_today,
        "daily_limit": config.get("workday", {}).get("daily_apply_limit", 20),
        "total_jobs": len(wd_jobs),
        "last_refresh": wd_data.get("last_run_at") or "Never",
        "session_status": "Active",
        "is_healthy": True,
        "supports_refresh": False
    })

    # Google / MS Forms
    fm_data, fm_jobs = _read_portal_file("forms_applied_jobs.json", job_key="applied_forms")
    fm_today = sum(1 for v in fm_jobs.values() if v.get("status") in ("APPLIED", "SUBMITTED") and str(v.get("applied_at", "")).startswith(today_str))
    portals_info.append({
        "key": "forms",
        "name": "Web Forms",
        "icon": "📋",
        "category": "Enterprise ATS",
        "applied_today": fm_today,
        "daily_limit": config.get("forms", {}).get("daily_apply_limit", 25),
        "total_jobs": len(fm_jobs),
        "last_refresh": fm_data.get("last_run_at") or "Never",
        "session_status": "Active",
        "is_healthy": True,
        "supports_refresh": False
    })

    # Cold Email Pipeline
    ce_path = os.path.join(LOGS_DIR, "sent_emails.json")
    ce_today = 0
    ce_total = 0
    if os.path.exists(ce_path):
        try:
            with open(ce_path, 'r', encoding='utf-8') as cf:
                ce_recs = json.load(cf)
                ce_total = len(ce_recs)
                ce_today = sum(1 for v in ce_recs.values() if str(v.get("sent_at", "")).startswith(today_str))
        except Exception:
            pass
    portals_info.append({
        "key": "cold_email",
        "name": "Cold Outreach",
        "icon": "📧",
        "category": "Direct Outreach",
        "applied_today": ce_today,
        "daily_limit": config.get("cold_email", {}).get("daily_send_limit", 25),
        "total_jobs": ce_total,
        "last_refresh": "10:00 AM - 6:00 PM IST Active",
        "session_status": "Active (SMTP)",
        "is_healthy": True,
        "supports_refresh": False
    })

    # 7. System Background Daemons (launchd)
    daily_loaded = False
    dropzone_loaded = False
    bot_loaded = False
    try:
        res = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=1.5)
        if res.returncode == 0:
            daily_loaded = "com.amrit.jobautomation.daily" in res.stdout
            dropzone_loaded = "com.amrit.jobautomation" in res.stdout
            bot_loaded = "com.amrit.jobautomation.bot" in res.stdout
    except Exception:
        pass

    # 8. Compile Urgent Attention Alerts
    urgent_alerts = []
    # Check for calls
    for c in call_tasks:
        urgent_alerts.append({
            "type": "CALL_SCHEDULE",
            "level": "CRITICAL",
            "title": f"Screening Call: {c.get('company')} ({c.get('role')})",
            "description": c.get('title'),
            "recruiter_email": c.get('recruiter_email'),
            "task_id": c.get('id'),
            "action_url": c.get('action_url')
        })

    # Check for session login alerts
    for p in portals_info:
        if not p.get("is_healthy") and p.get("session_status") == "Needs Login":
            urgent_alerts.append({
                "type": "SESSION_EXPIRED",
                "level": "WARNING",
                "title": f"{p['name']} Session Needs Re-Authentication",
                "description": f"Login session expired or unverified. Click to launch interactive login.",
                "portal_key": p['key']
            })

    # Check for stuck bot screenshots
    stuck_count = sum(1 for s in stuck_screenshots if s["is_stuck"])
    if stuck_count > 0:
        urgent_alerts.append({
            "type": "BOT_STUCK",
            "level": "WARNING",
            "title": f"{stuck_count} Bot Obstacles Detected (Screenshots Captured)",
            "description": "Portals encountered unexpected modal, captcha, or form questions needing review.",
            "count": stuck_count
        })

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "today_str": today_str,
        "quotas": quotas,
        "wave_info": wave_info,
        "tasks_summary": {
            "total_pending": len(pending_tasks),
            "high_priority": len(high_priority_tasks),
            "calls": len(call_tasks),
            "links": len(link_tasks),
            "reviews": len(review_tasks)
        },
        "drafts_summary": {
            "total_pending": len(pending_drafts)
        },
        "crm_summary": {
            "total_contacts": crm_summary.get("total_contacts", 0),
            "active_engagements": crm_summary.get("active_engagements", 0),
            "stage_counts": stage_counts
        },
        "portals": portals_info,
        "stuck_screenshots_count": len(stuck_screenshots),
        "urgent_alerts": urgent_alerts,
        "qa_summary": pending_qa.get_pending_stats(),
        "daemons": {
            "daily_scheduler": daily_loaded,
            "dropzone_watcher": dropzone_loaded,
            "telegram_bot": bot_loaded
        },
        "action_status": ACTION_RUNNER.get_status()
    }


# -----------------------------------------------------------------------------
# Web UI HTML Template
# -----------------------------------------------------------------------------

WEB_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>JobAutomation Mission Control</title>
  <meta name="theme-color" content="#0f172a">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="Mission Control">
  <link rel="manifest" href="/manifest.json">
  <link rel="icon" type="image/svg+xml" href="/assets/icon.svg">
  <link rel="alternate icon" type="image/png" href="/assets/favicon.png">
  <link rel="apple-touch-icon" href="/assets/icon-192.png">
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            brand: {
              50: '#eef2ff',
              100: '#e0e7ff',
              500: '#6366f1',
              600: '#4f46e5',
              700: '#4338ca',
            }
          }
        }
      }
    }
  </script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
    body { font-family: 'Inter', sans-serif; }
    code, pre { font-family: 'JetBrains Mono', monospace; }
    /* Custom Scrollbar for light theme */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #f1f5f9; }
    ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
  </style>
</head>
<body class="bg-slate-50 text-slate-800 min-h-screen flex flex-col antialiased selection:bg-brand-500 selection:text-white">

  <!-- TOP APP BAR -->
  <header class="border-b border-slate-200 bg-white/95 backdrop-blur sticky top-0 z-40 px-4 py-3 shadow-xs">
    <div class="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-3">
      
      <!-- Brand & Status Beacon -->
      <div class="flex items-center space-x-3">
        <div class="w-9 h-9 rounded-xl bg-gradient-to-tr from-indigo-600 to-violet-600 flex items-center justify-center shadow-md shadow-indigo-500/20 text-white">
          <span class="text-xl">🚀</span>
        </div>
        <div>
          <div class="flex items-center space-x-2">
            <h1 class="font-bold text-base tracking-tight text-slate-900">Mission Control</h1>
            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-50 text-emerald-700 border border-emerald-200">
              <span class="w-1.5 h-1.5 mr-1.5 rounded-full bg-emerald-500 animate-pulse"></span>
              Live Autonomous
            </span>
          </div>
          <p class="text-xs text-slate-500 font-medium">Amrit Kumar &bull; Full Stack AI &amp; Cloud Engine</p>
        </div>
      </div>

      <!-- Live Wave & Quota HUD Pills -->
      <div class="flex items-center flex-wrap gap-2 text-xs">
        <div class="bg-slate-100 border border-slate-200 rounded-lg px-3 py-1.5 flex items-center space-x-2 shadow-xs">
          <span class="text-slate-500">🌊 Schedule:</span>
          <span id="hudWaveText" class="font-semibold text-indigo-700">Loading Wave...</span>
        </div>
        <div class="bg-slate-100 border border-slate-200 rounded-lg px-3 py-1.5 flex items-center space-x-2 shadow-xs">
          <span class="text-slate-500">🎯 Daily Target:</span>
          <span id="hudQuotaText" class="font-semibold text-emerald-700">-- / 567</span>
          <div class="w-16 bg-slate-200 h-1.5 rounded-full overflow-hidden ml-1">
            <div id="hudQuotaBar" class="bg-emerald-500 h-full rounded-full transition-all duration-500" style="width: 0%"></div>
          </div>
        </div>
      </div>

      <!-- Global Actions -->
      <div class="flex items-center space-x-2">
        <button onclick="triggerQuickAction('wave')" id="btnWaveTrigger" class="bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold px-3 py-1.5 rounded-lg transition shadow-xs flex items-center space-x-1.5">
          <span>⚡</span>
          <span>Run Wave</span>
        </button>
        <button onclick="triggerQuickAction('refresh_all')" class="bg-white hover:bg-slate-50 text-slate-700 border border-slate-200 text-xs font-medium px-3 py-1.5 rounded-lg transition shadow-xs flex items-center space-x-1.5">
          <span>🔄</span>
          <span>Refresh All</span>
        </button>
        <button onclick="triggerQuickAction('inbound')" class="bg-white hover:bg-slate-50 text-slate-700 border border-slate-200 text-xs font-medium px-3 py-1.5 rounded-lg transition shadow-xs flex items-center space-x-1.5">
          <span>📥</span>
          <span>Inbound Sweep</span>
        </button>
        <button onclick="loadAllData(true)" title="Force Refresh Data" class="text-slate-500 hover:text-slate-800 p-1.5 rounded-lg hover:bg-slate-100 transition border border-slate-200 bg-white shadow-xs">
          <svg id="refreshSpinner" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
        </button>
      </div>

    </div>
  </header>

  <!-- BACKGROUND ACTION RUNNER STATUS BANNER (shown when command executing) -->
  <div id="actionRunningBanner" class="hidden bg-indigo-50 border-b border-indigo-200 px-4 py-2 text-xs text-indigo-700 font-medium">
    <div class="max-w-7xl mx-auto flex items-center justify-between">
      <div class="flex items-center space-x-2">
        <svg class="animate-spin h-3.5 w-3.5 text-indigo-600" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
        <span>Executing in background: <strong id="actionRunningText" class="text-indigo-900 font-semibold">...</strong></span>
      </div>
      <button onclick="switchTab('logsTab')" class="underline hover:text-indigo-900">View Output Terminal &rarr;</button>
    </div>
  </div>

  <!-- URGENT ATTENTION ALERTS CONTAINER -->
  <div id="urgentAlertsContainer" class="max-w-7xl mx-auto w-full px-4 pt-4"></div>

  <!-- MAIN NAVIGATION TABS -->
  <div class="border-b border-slate-200 bg-white/70 backdrop-blur px-4">
    <div class="max-w-7xl mx-auto flex space-x-1 overflow-x-auto text-sm">
      <button onclick="switchTab('overviewTab')" id="tabBtn-overviewTab" class="tab-btn px-4 py-3 font-semibold border-b-2 border-indigo-600 text-indigo-600 flex items-center space-x-2 whitespace-nowrap">
        <span>📊</span>
        <span>Executive HUD</span>
      </button>
      <button onclick="switchTab('triageTab')" id="tabBtn-triageTab" class="tab-btn px-4 py-3 font-medium border-b-2 border-transparent text-slate-500 hover:text-slate-800 flex items-center space-x-2 whitespace-nowrap">
        <span>📥</span>
        <span>Action Triage &amp; Drafts</span>
        <span id="tabBadgeDrafts" class="ml-1.5 px-2 py-0.5 rounded-full text-xs bg-indigo-50 text-indigo-700 border border-indigo-200 font-semibold">0</span>
      </button>
      <button onclick="switchTab('crmTab')" id="tabBtn-crmTab" class="tab-btn px-4 py-3 font-medium border-b-2 border-transparent text-slate-500 hover:text-slate-800 flex items-center space-x-2 whitespace-nowrap">
        <span>🤝</span>
        <span>Recruiter CRM</span>
        <span id="tabBadgeCRM" class="ml-1.5 px-2 py-0.5 rounded-full text-xs bg-slate-100 text-slate-600 border border-slate-200 font-semibold">238</span>
      </button>
      <button onclick="switchTab('portalsTab')" id="tabBtn-portalsTab" class="tab-btn px-4 py-3 font-medium border-b-2 border-transparent text-slate-500 hover:text-slate-800 flex items-center space-x-2 whitespace-nowrap">
        <span>🌐</span>
        <span>12-Engine Portals</span>
      </button>
      <button onclick="switchTab('debuggerTab')" id="tabBtn-debuggerTab" class="tab-btn px-4 py-3 font-medium border-b-2 border-transparent text-slate-500 hover:text-slate-800 flex items-center space-x-2 whitespace-nowrap">
        <span>📸</span>
        <span>Bot Debugger</span>
        <span id="tabBadgeScreenshots" class="ml-1.5 px-2 py-0.5 rounded-full text-xs bg-amber-50 text-amber-700 border border-amber-200 font-semibold">0</span>
      </button>
      <button onclick="switchTab('logsTab')" id="tabBtn-logsTab" class="tab-btn px-4 py-3 font-medium border-b-2 border-transparent text-slate-500 hover:text-slate-800 flex items-center space-x-2 whitespace-nowrap">
        <span>📜</span>
        <span>Live Telemetry</span>
      </button>
    </div>
  </div>

  <!-- MAIN TAB CONTENT CONTAINER -->
  <main class="max-w-7xl mx-auto w-full px-4 py-6 flex-1">

    <!-- ===================================================================== -->
    <!-- TAB 1: EXECUTIVE HUD & OVERVIEW -->
    <!-- ===================================================================== -->
    <div id="overviewTab" class="tab-content space-y-6">
      
      <!-- Top Metric Cards -->
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        
        <!-- Quota Progress -->
        <div class="bg-white border border-slate-200/90 rounded-xl p-4 shadow-xs hover:shadow transition">
          <div class="flex items-center justify-between text-slate-500 text-xs font-semibold mb-2">
            <span>Daily Quota (Wave Paced)</span>
            <span class="text-emerald-700 font-bold bg-emerald-50 px-2 py-0.5 rounded-full border border-emerald-200" id="cardQuotaPct">0%</span>
          </div>
          <div class="text-2xl font-bold text-slate-900 tracking-tight" id="cardQuotaMain">0 / 567</div>
          <p class="text-xs text-slate-500 mt-1" id="cardQuotaRemaining">554 actions left today</p>
          <div class="w-full bg-slate-100 h-2 rounded-full overflow-hidden mt-3 border border-slate-200/50">
            <div id="cardQuotaBar" class="bg-gradient-to-r from-emerald-500 to-teal-500 h-full rounded-full transition-all duration-500" style="width: 0%"></div>
          </div>
        </div>

        <!-- Recruiter Engagement -->
        <div class="bg-white border border-slate-200/90 rounded-xl p-4 shadow-xs hover:shadow transition">
          <div class="flex items-center justify-between text-slate-500 text-xs font-semibold mb-2">
            <span>Recruiter CRM Pipeline</span>
            <span class="text-indigo-700 font-bold bg-indigo-50 px-2 py-0.5 rounded-full border border-indigo-200" id="cardActiveEngagements">38 Active</span>
          </div>
          <div class="text-2xl font-bold text-slate-900 tracking-tight" id="cardTotalContacts">238 Leads</div>
          <p class="text-xs text-slate-500 mt-1" id="cardRepliesCalls">24 Replied &bull; 1 Call Request</p>
          <div class="flex items-center space-x-1.5 mt-3 text-xs">
            <span class="px-2 py-0.5 rounded bg-slate-100 text-slate-700 border border-slate-200 font-medium">200 Sent</span>
            <span class="px-2 py-0.5 rounded bg-emerald-50 text-emerald-700 border border-emerald-200 font-medium">24 Replied</span>
            <span class="px-2 py-0.5 rounded bg-amber-50 text-amber-800 border border-amber-200 font-medium">1 Call</span>
          </div>
        </div>

        <!-- Pending Tasks -->
        <div class="bg-white border border-slate-200/90 rounded-xl p-4 shadow-xs hover:shadow transition">
          <div class="flex items-center justify-between text-slate-500 text-xs font-semibold mb-2">
            <span>Action Items &amp; Tasks</span>
            <span class="text-rose-700 font-bold bg-rose-50 px-2 py-0.5 rounded-full border border-rose-200" id="cardHighPriorityTasks">38 Urgent</span>
          </div>
          <div class="text-2xl font-bold text-slate-900 tracking-tight" id="cardPendingTasks">43 Pending</div>
          <p class="text-xs text-slate-500 mt-1" id="cardTasksBreakdown">1 Call &bull; 14 Links &bull; 28 Reviews</p>
          <div class="mt-3">
            <button onclick="switchTab('triageTab')" class="text-xs text-indigo-600 hover:text-indigo-800 font-semibold">Triage items now &rarr;</button>
          </div>
        </div>

        <!-- Inbound Reply Drafts -->
        <div class="bg-white border border-slate-200/90 rounded-xl p-4 shadow-xs hover:shadow transition">
          <div class="flex items-center justify-between text-slate-500 text-xs font-semibold mb-2">
            <span>Inbound Email Drafts</span>
            <span class="text-amber-700 font-bold bg-amber-50 px-2 py-0.5 rounded-full border border-amber-200">Needs Review</span>
          </div>
          <div class="text-2xl font-bold text-slate-900 tracking-tight" id="cardPendingDrafts">42 Ready</div>
          <p class="text-xs text-slate-500 mt-1">AI-generated contextual responses</p>
          <div class="mt-3 flex items-center space-x-2">
            <button onclick="switchTab('triageTab')" class="text-xs bg-slate-100 hover:bg-slate-200 px-2.5 py-1 rounded-lg text-slate-700 border border-slate-200 font-medium transition">Review &amp; Send</button>
            <button onclick="batchApproveCTCReplies()" class="text-xs text-emerald-700 hover:text-emerald-800 font-semibold">Quick CTC Batch</button>
          </div>
        </div>

      </div>

      <!-- Quick Command Center & System Health Grid -->
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
        
        <!-- Quick Launcher Grid -->
        <div class="lg:col-span-2 bg-white border border-slate-200/90 rounded-xl p-5 shadow-xs space-y-4">
          <div class="flex items-center justify-between">
            <h2 class="font-bold text-slate-900 text-sm flex items-center space-x-2">
              <span>⚡</span>
              <span>Autonomous Action Launchpad</span>
            </h2>
            <span class="text-xs text-slate-400">Triggers safe background execution</span>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
            
            <button onclick="triggerQuickAction('wave')" class="p-3 bg-slate-50 hover:bg-indigo-50/60 border border-slate-200 hover:border-indigo-200 rounded-xl text-left transition group shadow-2xs">
              <div class="text-lg mb-1 group-hover:scale-110 transition-transform">🌊</div>
              <div class="font-semibold text-xs text-slate-900">Run Application Wave</div>
              <div class="text-[11px] text-slate-500 mt-0.5">Executes scheduled quota slice across all 10 portals</div>
            </button>

            <button onclick="triggerQuickAction('refresh_all')" class="p-3 bg-slate-50 hover:bg-indigo-50/60 border border-slate-200 hover:border-indigo-200 rounded-xl text-left transition group shadow-2xs">
              <div class="text-lg mb-1 group-hover:scale-110 transition-transform">🔄</div>
              <div class="font-semibold text-xs text-slate-900">Refresh All Profiles</div>
              <div class="text-[11px] text-slate-500 mt-0.5">Boosts search ranking on Naukri, Instahyre, Foundit</div>
            </button>

            <button onclick="triggerQuickAction('inbound')" class="p-3 bg-slate-50 hover:bg-indigo-50/60 border border-slate-200 hover:border-indigo-200 rounded-xl text-left transition group shadow-2xs">
              <div class="text-lg mb-1 group-hover:scale-110 transition-transform">📥</div>
              <div class="font-semibold text-xs text-slate-900">Inbound Harvester Sweep</div>
              <div class="text-[11px] text-slate-500 mt-0.5">Scans Gmail IMAP, extracts links, drafts responses</div>
            </button>

            <button onclick="triggerQuickAction('dropzone')" class="p-3 bg-slate-50 hover:bg-indigo-50/60 border border-slate-200 hover:border-indigo-200 rounded-xl text-left transition group shadow-2xs">
              <div class="text-lg mb-1 group-hover:scale-110 transition-transform">📂</div>
              <div class="font-semibold text-xs text-slate-900">Process Dropzone</div>
              <div class="text-[11px] text-slate-500 mt-0.5">Hydrates cloud files, OCR screenshots, sends emails</div>
            </button>

            <button onclick="triggerQuickAction('resume_editor')" class="p-3 bg-slate-50 hover:bg-indigo-50/60 border border-slate-200 hover:border-indigo-200 rounded-xl text-left transition group shadow-2xs">
              <div class="text-lg mb-1 group-hover:scale-110 transition-transform">📄</div>
              <div class="font-semibold text-xs text-slate-900">Launch Resume Editor</div>
              <div class="text-[11px] text-slate-500 mt-0.5">Opens 1-Page ATS compiler and preview tool</div>
            </button>

            <button onclick="triggerQuickAction('optimize')" class="p-3 bg-slate-50 hover:bg-indigo-50/60 border border-slate-200 hover:border-indigo-200 rounded-xl text-left transition group shadow-2xs">
              <div class="text-lg mb-1 group-hover:scale-110 transition-transform">🧠</div>
              <div class="font-semibold text-xs text-slate-900">Self-Learning Optimizer</div>
              <div class="text-[11px] text-slate-500 mt-0.5">Thompson sampling MAB &amp; query mutations</div>
            </button>

          </div>
        </div>

        <!-- System Telemetry & Daemons -->
        <div class="bg-white border border-slate-200/90 rounded-xl p-5 shadow-xs space-y-4">
          <h2 class="font-bold text-slate-900 text-sm flex items-center space-x-2">
            <span>🛡️</span>
            <span>Background Daemons &amp; Health</span>
          </h2>

          <div class="space-y-3 text-xs">
            <div class="flex items-center justify-between p-2.5 rounded-lg bg-slate-50 border border-slate-200">
              <div>
                <div class="font-semibold text-slate-800">5-Wave Daily Automation</div>
                <div class="text-[11px] text-slate-500 font-mono">com.amrit.jobautomation.daily</div>
              </div>
              <span id="badgeDaemonDaily" class="px-2 py-0.5 rounded text-[11px] bg-emerald-50 text-emerald-700 border border-emerald-200 font-medium">Active</span>
            </div>

            <div class="flex items-center justify-between p-2.5 rounded-lg bg-slate-50 border border-slate-200">
              <div>
                <div class="font-semibold text-slate-800">Dropzone Watcher (5 Min)</div>
                <div class="text-[11px] text-slate-500 font-mono">com.amrit.jobautomation</div>
              </div>
              <span id="badgeDaemonDropzone" class="px-2 py-0.5 rounded text-[11px] bg-emerald-50 text-emerald-700 border border-emerald-200 font-medium">Active</span>
            </div>

            <div class="flex items-center justify-between p-2.5 rounded-lg bg-slate-50 border border-slate-200">
              <div>
                <div class="font-semibold text-slate-800">Telegram 24/7 Command Bot</div>
                <div class="text-[11px] text-slate-500 font-mono">com.amrit.jobautomation.bot</div>
              </div>
              <span id="badgeDaemonBot" class="px-2 py-0.5 rounded text-[11px] bg-emerald-50 text-emerald-700 border border-emerald-200 font-medium">Active</span>
            </div>

            <div class="p-3 bg-indigo-50 border border-indigo-100 rounded-lg text-[11px] text-indigo-800">
              💡 <strong>Wave Schedule:</strong> 09:30 AM &bull; 11:30 AM &bull; 01:30 PM &bull; 03:30 PM &bull; 05:30 PM IST.
            </div>
          </div>
        </div>

      </div>

      <!-- Quick Recent Tasks Strip -->
      <div class="bg-white border border-slate-200/90 rounded-xl p-5 shadow-xs space-y-4">
        <div class="flex items-center justify-between">
          <h2 class="font-bold text-slate-900 text-sm flex items-center space-x-2">
            <span>📋</span>
            <span>Immediate Candidate Action Items</span>
          </h2>
          <button onclick="switchTab('triageTab')" class="text-xs text-indigo-600 hover:text-indigo-800 font-semibold">View all 43 items &rarr;</button>
        </div>

        <div id="overviewTasksList" class="space-y-2">
          <!-- Dynamically populated -->
        </div>
      </div>

    </div>

    <!-- ===================================================================== -->
    <!-- TAB 2: TRIAGE CENTER & EMAIL DRAFT APPROVER -->
    <!-- ===================================================================== -->
    <div id="triageTab" class="tab-content hidden space-y-6">
      
      <!-- Top Triage Controls -->
      <div class="flex flex-wrap items-center justify-between gap-3 bg-white border border-slate-200 p-4 rounded-xl shadow-xs">
        <div class="flex items-center space-x-3">
          <h2 class="font-bold text-slate-900 text-base">Inbound Recruiter Draft Reviewer</h2>
          <span id="draftCountBadge" class="px-2 py-0.5 rounded-full text-xs bg-indigo-50 text-indigo-700 border border-indigo-200 font-semibold">42 Pending Drafts</span>
        </div>
        <div class="flex items-center space-x-2">
          <button onclick="batchApproveCTCReplies()" class="bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-semibold px-3 py-1.5 rounded-lg transition shadow-xs flex items-center space-x-1.5">
            <span>⚡</span>
            <span>Batch Approve Standard CTC &amp; Notice Replies</span>
          </button>
        </div>
      </div>

      <!-- Split Screen Draft Reviewer -->
      <div class="grid grid-cols-1 lg:grid-cols-12 gap-6 min-h-[580px]">
        
        <!-- Left Pane: Drafts List -->
        <div class="lg:col-span-5 bg-white border border-slate-200 rounded-xl flex flex-col overflow-hidden max-h-[650px] shadow-xs">
          <div class="p-3 border-b border-slate-200 bg-slate-50 flex items-center justify-between">
            <span class="text-xs font-semibold text-slate-700">Pending Recruiter Responses</span>
            <input type="text" id="draftSearchInput" oninput="filterDraftsList()" placeholder="Filter by company or recruiter..." class="bg-white border border-slate-200 rounded-lg text-xs px-2.5 py-1 text-slate-800 focus:outline-none focus:border-indigo-500 w-48 shadow-2xs">
          </div>
          <div id="draftsListContainer" class="divide-y divide-slate-100 overflow-y-auto flex-1">
            <!-- Dynamic Draft Items -->
          </div>
        </div>

        <!-- Right Pane: Active Draft Detail & Editor -->
        <div class="lg:col-span-7 bg-white border border-slate-200 rounded-xl p-5 flex flex-col justify-between shadow-xs">
          
          <div id="draftDetailEmptyState" class="my-auto text-center py-12 text-slate-400">
            <div class="text-4xl mb-2">✉️</div>
            <p class="text-sm font-medium">Select a recruiter reply draft from the list to review, edit, or approve.</p>
          </div>

          <div id="draftDetailPane" class="hidden space-y-4">
            
            <!-- Recruiter Meta Header -->
            <div class="border-b border-slate-200 pb-3 flex items-start justify-between">
              <div>
                <div class="flex items-center space-x-2">
                  <span id="draftDetailCompany" class="font-bold text-slate-900 text-base">Company Name</span>
                  <span id="draftDetailIntentBadge" class="text-[10px] px-2 py-0.5 rounded font-mono uppercase bg-indigo-50 text-indigo-700 border border-indigo-200 font-semibold">INTENT</span>
                </div>
                <div class="text-xs text-slate-500 mt-0.5 flex items-center space-x-2">
                  <span>To: <strong id="draftDetailRecruiter" class="text-slate-800">recruiter@company.com</strong></span>
                  <span>&bull;</span>
                  <span id="draftDetailRole">Role Title</span>
                </div>
              </div>
              <span id="draftDetailId" class="text-xs font-mono text-slate-500 bg-slate-50 px-2 py-1 rounded border border-slate-200">DRAFT-ID</span>
            </div>

            <!-- Subject Line -->
            <div>
              <label class="block text-xs font-semibold text-slate-600 mb-1">Subject Line</label>
              <input type="text" id="draftEditSubject" class="w-full bg-slate-50 border border-slate-200 rounded-lg text-xs px-3 py-2 text-slate-900 focus:bg-white focus:outline-none focus:border-indigo-500 shadow-2xs">
            </div>

            <!-- Email Body Editor -->
            <div class="flex-1">
              <label class="block text-xs font-semibold text-slate-600 mb-1">AI Generated Reply Content (Context-Aware)</label>
              <textarea id="draftEditBody" rows="12" class="w-full bg-slate-50 border border-slate-200 rounded-lg text-xs p-3 text-slate-800 font-mono leading-relaxed focus:bg-white focus:outline-none focus:border-indigo-500 shadow-2xs"></textarea>
            </div>

            <!-- Action Buttons -->
            <div class="flex items-center justify-between pt-3 border-t border-slate-200">
              <div class="flex items-center space-x-2">
                <button onclick="dismissCurrentDraft()" class="text-xs text-slate-500 hover:text-rose-600 px-3 py-2 rounded transition font-medium">
                  Reject &amp; Archive
                </button>
              </div>
              <div class="flex items-center space-x-2">
                <button onclick="saveCurrentDraftEdits()" class="bg-slate-100 hover:bg-slate-200 text-slate-700 border border-slate-200 text-xs font-semibold px-4 py-2 rounded-lg transition shadow-xs">
                  Save Changes
                </button>
                <button onclick="approveCurrentDraft()" id="btnApproveDraft" class="bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold px-4 py-2 rounded-lg transition shadow-xs flex items-center space-x-1.5">
                  <span>🚀</span>
                  <span>Approve &amp; Send via Gmail SMTP</span>
                </button>
              </div>
            </div>

          </div>

        </div>

      </div>

      <!-- Candidate Task Ledger Section -->
      <div class="bg-white border border-slate-200 rounded-xl p-5 shadow-xs space-y-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 class="font-bold text-slate-900 text-sm flex items-center space-x-2">
              <span>📋</span>
              <span>Candidate Action Items &amp; Recruiter Follow-ups</span>
            </h2>
            <p class="text-xs text-slate-500">Captured obligations from recruiter emails, external links, and interview invites</p>
          </div>

          <div class="flex items-center space-x-1.5 text-xs">
            <button onclick="filterTasks('ALL')" class="task-filter-btn px-2.5 py-1 rounded-lg bg-indigo-600 text-white font-semibold shadow-xs" data-filter="ALL">All (43)</button>
            <button onclick="filterTasks('HIGH')" class="task-filter-btn px-2.5 py-1 rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200 font-medium" data-filter="HIGH">Urgent (38)</button>
            <button onclick="filterTasks('SCHEDULE_CALL')" class="task-filter-btn px-2.5 py-1 rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200 font-medium" data-filter="SCHEDULE_CALL">Calls (1)</button>
            <button onclick="filterTasks('APPLY_LINK_MANUAL')" class="task-filter-btn px-2.5 py-1 rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200 font-medium" data-filter="APPLY_LINK_MANUAL">Links (14)</button>
          </div>
        </div>

        <div id="tasksTableContainer" class="overflow-x-auto">
          <!-- Dynamic Tasks List -->
        </div>
      </div>

    </div>

    <!-- ===================================================================== -->
    <!-- TAB 3: RECRUITER RELATIONSHIP CRM KANBAN -->
    <!-- ===================================================================== -->
    <div id="crmTab" class="tab-content hidden space-y-4">
      
      <!-- CRM Header Controls -->
      <div class="flex flex-wrap items-center justify-between gap-3 bg-white border border-slate-200 p-4 rounded-xl shadow-xs">
        <div>
          <h2 class="font-bold text-slate-900 text-base">Recruiter Relationship CRM Pipeline</h2>
          <p class="text-xs text-slate-500">Tracking 238 recruiter threads across communication stages</p>
        </div>
        <div class="flex items-center space-x-2">
          <input type="text" id="crmSearchInput" oninput="renderCrmKanban()" placeholder="Search by company, recruiter, or role..." class="bg-slate-50 border border-slate-200 rounded-lg text-xs px-3 py-1.5 text-slate-800 focus:bg-white focus:outline-none focus:border-indigo-500 w-64 shadow-2xs">
        </div>
      </div>

      <!-- Kanban Columns Container -->
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4 overflow-x-auto pb-4">
        
        <!-- Column 1: Outreach Sent -->
        <div class="bg-slate-100/70 border border-slate-200/90 rounded-xl p-3 flex flex-col h-[650px] shadow-xs">
          <div class="flex items-center justify-between pb-2 border-b border-slate-200 mb-3">
            <span class="text-xs font-semibold text-slate-700 flex items-center space-x-1.5">
              <span>📤</span>
              <span>Outreach Sent</span>
            </span>
            <span id="crmColCountSent" class="text-xs px-2 py-0.5 rounded-full bg-white text-slate-600 font-mono border border-slate-200 shadow-2xs">0</span>
          </div>
          <div id="crmColSent" class="space-y-2 overflow-y-auto flex-1 pr-1"></div>
        </div>

        <!-- Column 2: Replied -->
        <div class="bg-slate-100/70 border border-slate-200/90 rounded-xl p-3 flex flex-col h-[650px] shadow-xs">
          <div class="flex items-center justify-between pb-2 border-b border-slate-200 mb-3">
            <span class="text-xs font-semibold text-emerald-800 flex items-center space-x-1.5">
              <span>💬</span>
              <span>Replied</span>
            </span>
            <span id="crmColCountReplied" class="text-xs px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 font-mono shadow-2xs">0</span>
          </div>
          <div id="crmColReplied" class="space-y-2 overflow-y-auto flex-1 pr-1"></div>
        </div>

        <!-- Column 3: Link Received -->
        <div class="bg-slate-100/70 border border-slate-200/90 rounded-xl p-3 flex flex-col h-[650px] shadow-xs">
          <div class="flex items-center justify-between pb-2 border-b border-slate-200 mb-3">
            <span class="text-xs font-semibold text-indigo-800 flex items-center space-x-1.5">
              <span>🔗</span>
              <span>Link Received</span>
            </span>
            <span id="crmColCountLink" class="text-xs px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-700 border border-indigo-200 font-mono shadow-2xs">0</span>
          </div>
          <div id="crmColLink" class="space-y-2 overflow-y-auto flex-1 pr-1"></div>
        </div>

        <!-- Column 4: Screening Requested -->
        <div class="bg-slate-100/70 border border-slate-200/90 rounded-xl p-3 flex flex-col h-[650px] shadow-xs">
          <div class="flex items-center justify-between pb-2 border-b border-slate-200 mb-3">
            <span class="text-xs font-semibold text-amber-800 flex items-center space-x-1.5">
              <span>📞</span>
              <span>Screening Requested</span>
            </span>
            <span id="crmColCountScreening" class="text-xs px-2 py-0.5 rounded-full bg-amber-50 text-amber-800 border border-amber-200 font-mono shadow-2xs">0</span>
          </div>
          <div id="crmColScreening" class="space-y-2 overflow-y-auto flex-1 pr-1"></div>
        </div>

        <!-- Column 5: Interview / Offer -->
        <div class="bg-slate-100/70 border border-slate-200/90 rounded-xl p-3 flex flex-col h-[650px] shadow-xs">
          <div class="flex items-center justify-between pb-2 border-b border-slate-200 mb-3">
            <span class="text-xs font-semibold text-purple-800 flex items-center space-x-1.5">
              <span>🏆</span>
              <span>Interview / Offer</span>
            </span>
            <span id="crmColCountOffer" class="text-xs px-2 py-0.5 rounded-full bg-purple-50 text-purple-800 border border-purple-200 font-mono shadow-2xs">0</span>
          </div>
          <div id="crmColOffer" class="space-y-2 overflow-y-auto flex-1 pr-1"></div>
        </div>

      </div>

    </div>

    <!-- ===================================================================== -->
    <!-- TAB 4: 12-ENGINE PORTAL MATRIX -->
    <!-- ===================================================================== -->
    <div id="portalsTab" class="tab-content hidden space-y-6">
      
      <div class="flex flex-wrap items-center justify-between gap-3 bg-white border border-slate-200 p-4 rounded-xl shadow-xs">
        <div>
          <h2 class="font-bold text-slate-900 text-base">Autonomous Application Portals &amp; ATS Matrix</h2>
          <p class="text-xs text-slate-500">12 dedicated automation engines managing search, profile freshness, and multi-step applications</p>
        </div>
        <button onclick="triggerQuickAction('refresh_all')" class="bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold px-3 py-1.5 rounded-lg transition shadow-xs">
          Refresh All Supported Portals
        </button>
      </div>

      <!-- Portal Grid -->
      <div id="portalsGridContainer" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <!-- Dynamically Populated Cards -->
      </div>

    </div>

    <!-- ===================================================================== -->
    <!-- TAB 5: VISUAL BOT DEBUGGER & SCREENSHOTS -->
    <!-- ===================================================================== -->
    <div id="debuggerTab" class="tab-content hidden space-y-6">
      
      <div class="flex flex-wrap items-center justify-between gap-3 bg-white border border-slate-200 p-4 rounded-xl shadow-xs">
        <div>
          <h2 class="font-bold text-slate-900 text-base">Bot Visual Telemetry &amp; Stuck Resolvers</h2>
          <p class="text-xs text-slate-500">Inspect screenshots captured during auto-apply execution obstacles</p>
        </div>
        <span id="debuggerCountBadge" class="text-xs px-2.5 py-1 rounded-full bg-slate-100 text-slate-700 border border-slate-200 font-semibold">0 captures</span>
      </div>

      <div id="screenshotsGridContainer" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        <!-- Dynamically Populated Screenshots -->
      </div>

    </div>

    <!-- ===================================================================== -->
    <!-- TAB 6: LIVE LOGS & TELEMETRY -->
    <!-- ===================================================================== -->
    <div id="logsTab" class="tab-content hidden space-y-4">
      
      <div class="flex flex-wrap items-center justify-between gap-3 bg-white border border-slate-200 p-4 rounded-xl shadow-xs">
        <div class="flex items-center space-x-3">
          <h2 class="font-bold text-slate-900 text-base">System Telemetry &amp; Event Stream</h2>
          <span class="inline-flex items-center px-2 py-0.5 rounded text-xs bg-slate-100 text-slate-700 border border-slate-200 font-mono">automation.log</span>
        </div>
        <div class="flex items-center space-x-2">
          <button onclick="fetchLiveLogs()" class="bg-slate-100 hover:bg-slate-200 text-slate-700 border border-slate-200 text-xs font-semibold px-3 py-1.5 rounded-lg transition shadow-xs">
            Refresh Log
          </button>
        </div>
      </div>

      <div class="bg-slate-900 border border-slate-300 rounded-xl p-4 font-mono text-xs overflow-x-auto h-[600px] flex flex-col justify-end shadow-inner">
        <div id="terminalLogOutput" class="space-y-1 text-slate-200 overflow-y-auto">
          Loading system logs...
        </div>
      </div>

    </div>

  </main>

  <!-- LIGHTBOX IMAGE MODAL -->
  <div id="imageLightboxModal" class="fixed inset-0 z-50 bg-black/60 hidden backdrop-blur-sm flex items-center justify-center p-4" onclick="closeLightbox()">
    <div class="relative max-w-5xl max-h-[90vh] bg-white border border-slate-200 rounded-xl overflow-hidden p-3 shadow-2xl" onclick="event.stopPropagation()">
      <div class="flex items-center justify-between p-2 border-b border-slate-200 text-xs text-slate-700">
        <span id="lightboxTitle" class="font-semibold text-slate-900">Screenshot Preview</span>
        <button onclick="closeLightbox()" class="text-slate-400 hover:text-slate-800 text-lg px-2 font-bold">&times;</button>
      </div>
      <div class="overflow-auto max-h-[80vh] flex items-center justify-center p-2 bg-slate-50 rounded-lg mt-2">
        <img id="lightboxImg" src="" alt="Screenshot Preview" class="rounded max-h-full max-w-full object-contain shadow-xs">
      </div>
    </div>
  </div>

  <!-- TOAST NOTIFICATION -->
  <div id="toastContainer" class="fixed bottom-4 right-4 z-50 flex flex-col space-y-2 pointer-events-none"></div>

  <!-- CLIENT-SIDE SCRIPT -->
  <script>
    let globalState = null;
    let globalDrafts = [];
    let globalTasks = [];
    let globalCrm = [];
    let activeDraftId = null;
    let currentTaskFilter = 'ALL';

    // TAB SWITCHING
    function switchTab(tabId) {
      document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
      document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('border-indigo-600', 'text-indigo-600', 'font-semibold');
        btn.classList.add('border-transparent', 'text-slate-500');
      });

      const target = document.getElementById(tabId);
      const btn = document.getElementById('tabBtn-' + tabId);
      if (target) target.classList.remove('hidden');
      if (btn) {
        btn.classList.remove('border-transparent', 'text-slate-500');
        btn.classList.add('border-indigo-600', 'text-indigo-600', 'font-semibold');
      }

      if (tabId === 'logsTab') fetchLiveLogs();
      if (tabId === 'debuggerTab') fetchScreenshots();
    }

    // TOAST MESSAGES
    function showToast(message, type = 'info') {
      const container = document.getElementById('toastContainer');
      const toast = document.createElement('div');
      const bg = type === 'success' ? 'bg-emerald-50 border-emerald-200 text-emerald-900' :
                 type === 'error' ? 'bg-rose-50 border-rose-200 text-rose-900' :
                 'bg-white border-slate-200 text-slate-800';
      toast.className = `${bg} border px-4 py-2.5 rounded-xl shadow-lg text-xs font-medium pointer-events-auto transition-all duration-300 transform translate-y-2 opacity-0 flex items-center space-x-2`;
      toast.innerHTML = `<span>${type === 'success' ? '✓' : type === 'error' ? '⚠️' : 'ℹ️'}</span><span>${message}</span>`;
      container.appendChild(toast);
      setTimeout(() => { toast.classList.remove('translate-y-2', 'opacity-0'); }, 10);
      setTimeout(() => {
        toast.classList.add('opacity-0', 'translate-y-2');
        setTimeout(() => toast.remove(), 300);
      }, 3500);
    }

    // LOAD ALL DATA
    async function loadAllData(showSuccessToast = false) {
      const spinner = document.getElementById('refreshSpinner');
      if (spinner) spinner.classList.add('animate-spin');

      try {
        const ts = Date.now();
        const [stateRes, draftsRes, tasksRes, crmRes] = await Promise.all([
          fetch(`/api/state?_t=${ts}`, { cache: 'no-store' }).then(r => r.json()),
          fetch(`/api/drafts?_t=${ts}`, { cache: 'no-store' }).then(r => r.json()),
          fetch(`/api/tasks?_t=${ts}`, { cache: 'no-store' }).then(r => r.json()),
          fetch(`/api/crm?_t=${ts}`, { cache: 'no-store' }).then(r => r.json())
        ]);

        globalState = stateRes;
        globalDrafts = draftsRes.drafts || [];
        globalTasks = tasksRes.tasks || [];
        globalCrm = crmRes.threads || [];

        renderHUD();
        renderUrgentAlerts();
        renderOverviewTasks();
        renderDraftsList();
        renderTasksList();
        renderCrmKanban();
        renderPortalsGrid();

        if (showSuccessToast) showToast('System state synchronized', 'success');
      } catch (err) {
        console.error('Failed to load data:', err);
        showToast('Error syncing system state', 'error');
      } finally {
        if (spinner) spinner.classList.remove('animate-spin');
      }
    }

    // RENDER TOP HUD
    function renderHUD() {
      if (!globalState) return;

      const q = globalState.quotas || {};
      const w = globalState.wave_info || {};
      const pct = q.overall_percent || 0;

      // HUD Top Bar
      const hudWaveText = document.getElementById('hudWaveText');
      if (hudWaveText) {
        hudWaveText.innerText = w.current_wave_num > 0 ?
          `Wave ${w.current_wave_num} of ${w.total_waves} (Next: ${w.next_wave_str})` : 'Pre-Wave 1';
      }

      const hudQuotaText = document.getElementById('hudQuotaText');
      const hudQuotaBar = document.getElementById('hudQuotaBar');
      if (hudQuotaText) hudQuotaText.innerText = `${q.total_applied || 0} / ${q.total_limit || 567} (${pct}%)`;
      if (hudQuotaBar) hudQuotaBar.style.width = `${pct}%`;

      // Card Metrics
      const cardQuotaMain = document.getElementById('cardQuotaMain');
      const cardQuotaPct = document.getElementById('cardQuotaPct');
      const cardQuotaRemaining = document.getElementById('cardQuotaRemaining');
      const cardQuotaBar = document.getElementById('cardQuotaBar');

      if (cardQuotaMain) cardQuotaMain.innerText = `${q.total_applied || 0} / ${q.total_limit || 567}`;
      if (cardQuotaPct) cardQuotaPct.innerText = `${pct}%`;
      if (cardQuotaRemaining) cardQuotaRemaining.innerText = `${q.total_remaining || 0} applications left today`;
      if (cardQuotaBar) cardQuotaBar.style.width = `${pct}%`;

      // CRM Card
      const crm = globalState.crm_summary || {};
      const cardTotalContacts = document.getElementById('cardTotalContacts');
      const cardActiveEngagements = document.getElementById('cardActiveEngagements');
      const cardRepliesCalls = document.getElementById('cardRepliesCalls');
      if (cardTotalContacts) cardTotalContacts.innerText = `${crm.total_contacts || 238} Leads`;
      if (cardActiveEngagements) cardActiveEngagements.innerText = `${crm.active_engagements || 38} Active`;
      if (cardRepliesCalls) {
        const sc = crm.stage_counts || {};
        cardRepliesCalls.innerText = `${sc.REPLIED || 24} Replied &bull; ${sc.SCREENING_REQUESTED || 1} Call Request`;
      }

      // Tasks Card
      const ts = globalState.tasks_summary || {};
      const cardPendingTasks = document.getElementById('cardPendingTasks');
      const cardHighPriorityTasks = document.getElementById('cardHighPriorityTasks');
      const cardTasksBreakdown = document.getElementById('cardTasksBreakdown');
      if (cardPendingTasks) cardPendingTasks.innerText = `${ts.total_pending || 0} Pending`;
      if (cardHighPriorityTasks) cardHighPriorityTasks.innerText = `${ts.high_priority || 0} Urgent`;
      if (cardTasksBreakdown) cardTasksBreakdown.innerHTML = `${ts.calls || 0} Call &bull; ${ts.links || 0} Links &bull; ${ts.reviews || 0} Reviews`;

      // Drafts Card
      const ds = globalState.drafts_summary || {};
      const cardPendingDrafts = document.getElementById('cardPendingDrafts');
      if (cardPendingDrafts) cardPendingDrafts.innerText = `${ds.total_pending || 0} Ready`;

      // Badges
      const tabBadgeDrafts = document.getElementById('tabBadgeDrafts');
      const tabBadgeCRM = document.getElementById('tabBadgeCRM');
      const draftCountBadge = document.getElementById('draftCountBadge');
      if (tabBadgeDrafts) tabBadgeDrafts.innerText = ds.total_pending || 0;
      if (draftCountBadge) draftCountBadge.innerText = `${ds.total_pending || 0} Pending Drafts`;
      if (tabBadgeCRM) tabBadgeCRM.innerText = crm.total_contacts || 238;

      // Action Runner Banner
      const action = globalState.action_status || {};
      const actionBanner = document.getElementById('actionRunningBanner');
      const actionText = document.getElementById('actionRunningText');
      if (action.is_running) {
        actionBanner.classList.remove('hidden');
        actionText.innerText = action.current_action || 'Automated Task';
      } else {
        actionBanner.classList.add('hidden');
      }

      // Daemons
      const d = globalState.daemons || {};
      const bDaily = document.getElementById('badgeDaemonDaily');
      const bDrop = document.getElementById('badgeDaemonDropzone');
      const bBot = document.getElementById('badgeDaemonBot');
      if (bDaily) bDaily.className = d.daily_scheduler ? 'px-2 py-0.5 rounded text-[11px] bg-emerald-50 text-emerald-700 border border-emerald-200 font-medium' : 'px-2 py-0.5 rounded text-[11px] bg-slate-100 text-slate-500 border border-slate-200 font-medium';
      if (bDrop) bDrop.className = d.dropzone_watcher ? 'px-2 py-0.5 rounded text-[11px] bg-emerald-50 text-emerald-700 border border-emerald-200 font-medium' : 'px-2 py-0.5 rounded text-[11px] bg-slate-100 text-slate-500 border border-slate-200 font-medium';
      if (bBot) bBot.className = d.telegram_bot ? 'px-2 py-0.5 rounded text-[11px] bg-emerald-50 text-emerald-700 border border-emerald-200 font-medium' : 'px-2 py-0.5 rounded text-[11px] bg-slate-100 text-slate-500 border border-slate-200 font-medium';
    }

    // RENDER URGENT ALERTS
    function renderUrgentAlerts() {
      const container = document.getElementById('urgentAlertsContainer');
      if (!container || !globalState) return;

      const alerts = globalState.urgent_alerts || [];
      if (alerts.length === 0) {
        container.innerHTML = '';
        return;
      }

      let html = '<div class="space-y-2">';
      alerts.forEach(a => {
        const isCrit = a.level === 'CRITICAL';
        const bg = isCrit ? 'bg-rose-50 border-rose-200 text-rose-900' : 'bg-amber-50 border-amber-200 text-amber-900';
        const icon = isCrit ? '🚨' : '⚠️';
        
        let actionBtn = '';
        if (a.type === 'CALL_SCHEDULE') {
          actionBtn = `<button onclick="switchTab('triageTab')" class="text-xs bg-rose-600 hover:bg-rose-700 text-white font-semibold px-3 py-1 rounded-lg transition shadow-xs">Respond to Call &rarr;</button>`;
        } else if (a.type === 'SESSION_EXPIRED') {
          actionBtn = `<button onclick="triggerQuickAction('login_${a.portal_key}')" class="text-xs bg-amber-600 hover:bg-amber-700 text-white font-semibold px-3 py-1 rounded-lg transition shadow-xs">Re-Login Now &rarr;</button>`;
        } else if (a.type === 'BOT_STUCK') {
          actionBtn = `<button onclick="switchTab('debuggerTab')" class="text-xs bg-white hover:bg-slate-50 text-slate-800 font-semibold px-3 py-1 rounded-lg border border-slate-200 shadow-xs transition">Inspect Screenshots &rarr;</button>`;
        }

        html += `
          <div class="p-3.5 rounded-xl border ${bg} flex flex-wrap items-center justify-between gap-3 shadow-xs">
            <div class="flex items-center space-x-3">
              <span class="text-xl">${icon}</span>
              <div>
                <strong class="text-slate-900 text-xs font-bold block">${a.title}</strong>
                <span class="text-xs opacity-90">${a.description}</span>
              </div>
            </div>
            ${actionBtn}
          </div>
        `;
      });
      html += '</div>';
      container.innerHTML = html;
    }

    // RENDER OVERVIEW TASKS
    function renderOverviewTasks() {
      const container = document.getElementById('overviewTasksList');
      if (!container) return;

      const topTasks = globalTasks.filter(t => t.status === 'PENDING').slice(0, 5);
      if (topTasks.length === 0) {
        container.innerHTML = '<div class="text-xs text-slate-400 py-3">No pending candidate tasks! All caught up.</div>';
        return;
      }

      let html = '';
      topTasks.forEach(t => {
        const isCall = t.type === 'SCHEDULE_CALL';
        const isHigh = t.priority === 'HIGH';
        const pBadge = isCall ? 'bg-rose-50 text-rose-700 border-rose-200' :
                       isHigh ? 'bg-amber-50 text-amber-700 border-amber-200' : 'bg-slate-100 text-slate-600 border-slate-200';

        html += `
          <div class="flex items-center justify-between p-3 rounded-lg bg-slate-50 border border-slate-200 hover:border-slate-300 transition shadow-2xs">
            <div class="flex items-center space-x-3 min-w-0">
              <span class="text-sm">${isCall ? '📞' : '📋'}</span>
              <div class="min-w-0">
                <div class="flex items-center space-x-2">
                  <span class="text-xs font-semibold text-slate-900 truncate">${t.title}</span>
                  <span class="text-[10px] px-1.5 py-0.2 rounded font-mono uppercase border ${pBadge} font-medium">${t.priority}</span>
                </div>
                <div class="text-[11px] text-slate-500 mt-0.5 truncate">${t.company} &bull; ${t.recruiter_email || 'No email'}</div>
              </div>
            </div>
            <div class="flex items-center space-x-2 shrink-0">
              ${t.action_url ? `<a href="${t.action_url}" target="_blank" class="text-xs text-indigo-700 hover:text-indigo-900 font-semibold px-2 py-1 bg-white border border-slate-200 rounded-lg shadow-xs">Open Link ↗</a>` : ''}
              <button onclick="completeTask('${t.id}')" class="text-xs bg-emerald-50 text-emerald-700 border border-emerald-200 hover:bg-emerald-100 font-semibold px-2.5 py-1 rounded-lg transition shadow-2xs">Mark Done</button>
            </div>
          </div>
        `;
      });
      container.innerHTML = html;
    }

    // RENDER DRAFTS LIST
    function renderDraftsList() {
      const container = document.getElementById('draftsListContainer');
      if (!container) return;

      const q = (document.getElementById('draftSearchInput')?.value || '').toLowerCase();
      const filtered = globalDrafts.filter(d => {
        if (!q) return true;
        return (d.company || '').toLowerCase().includes(q) ||
               (d.recruiter_email || '').toLowerCase().includes(q) ||
               (d.subject || '').toLowerCase().includes(q);
      });

      if (filtered.length === 0) {
        container.innerHTML = '<div class="p-8 text-center text-xs text-slate-400 font-medium">No matching drafts found.</div>';
        return;
      }

      let html = '';
      filtered.forEach(d => {
        const isCurrent = d.draft_id === activeDraftId;
        const activeClass = isCurrent ? 'bg-indigo-50/90 border-l-4 border-indigo-600' : 'hover:bg-slate-50';

        html += `
          <div onclick="selectDraft('${d.draft_id}')" class="p-3.5 cursor-pointer transition ${activeClass}">
            <div class="flex items-center justify-between mb-1">
              <strong class="text-xs font-bold text-slate-900 truncate max-w-[180px]">${d.company || 'Direct Recruiter'}</strong>
              <span class="text-[10px] px-1.5 py-0.2 rounded font-mono uppercase bg-slate-100 text-slate-700 border border-slate-200 font-medium">${d.intent || 'REPLY'}</span>
            </div>
            <div class="text-[11px] text-slate-600 truncate font-medium">${d.subject || 'Application Reply'}</div>
            <div class="text-[10px] text-slate-400 mt-1 flex items-center justify-between">
              <span class="truncate max-w-[160px]">${d.recruiter_email || ''}</span>
              <span>${(d.created_at || '').substring(5, 16)}</span>
            </div>
          </div>
        `;
      });
      container.innerHTML = html;

      // Auto-select first draft if none selected
      if (!activeDraftId && filtered.length > 0) {
        selectDraft(filtered[0].draft_id);
      }
    }

    function filterDraftsList() {
      renderDraftsList();
    }

    function selectDraft(draftId) {
      activeDraftId = draftId;
      const draft = globalDrafts.find(d => d.draft_id === draftId);
      if (!draft) return;

      renderDraftsList();

      document.getElementById('draftDetailEmptyState').classList.add('hidden');
      document.getElementById('draftDetailPane').classList.remove('hidden');

      document.getElementById('draftDetailCompany').innerText = draft.company || 'Recruiter Direct';
      document.getElementById('draftDetailRecruiter').innerText = draft.recruiter_email || 'recruiter';
      document.getElementById('draftDetailRole').innerText = draft.role || 'Software Engineer';
      document.getElementById('draftDetailId').innerText = draft.draft_id;
      document.getElementById('draftDetailIntentBadge').innerText = draft.intent || 'GENERAL_INQUIRY';
      document.getElementById('draftEditSubject').value = draft.subject || '';
      document.getElementById('draftEditBody').value = draft.body || '';
    }

    async function approveCurrentDraft() {
      if (!activeDraftId) return;
      const btn = document.getElementById('btnApproveDraft');
      btn.disabled = true;
      btn.innerHTML = '<span>⏳ Sending via SMTP...</span>';

      try {
        const res = await fetch('/api/drafts/approve', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ draft_id: activeDraftId })
        }).then(r => r.json());

        if (res.success) {
          showToast(`Reply sent successfully to recruiter!`, 'success');
          globalDrafts = globalDrafts.filter(d => d.draft_id !== activeDraftId);
          activeDraftId = null;
          loadAllData();
        } else {
          showToast(`Failed to send: ${res.error || 'Check SMTP config'}`, 'error');
        }
      } catch (e) {
        showToast('Error approving draft', 'error');
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<span>🚀</span><span>Approve &amp; Send via Gmail SMTP</span>';
      }
    }

    async function saveCurrentDraftEdits() {
      if (!activeDraftId) return;
      const subject = document.getElementById('draftEditSubject').value;
      const body = document.getElementById('draftEditBody').value;

      try {
        const res = await fetch('/api/drafts/update', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ draft_id: activeDraftId, subject, body })
        }).then(r => r.json());

        if (res.success) {
          showToast('Draft edits saved locally', 'success');
          const d = globalDrafts.find(item => item.draft_id === activeDraftId);
          if (d) { d.subject = subject; d.body = body; }
        } else {
          showToast('Failed to save draft', 'error');
        }
      } catch (e) {
        showToast('Error saving draft', 'error');
      }
    }

    async function dismissCurrentDraft() {
      if (!activeDraftId || !confirm('Are you sure you want to dismiss this draft?')) return;
      try {
        const res = await fetch('/api/drafts/reject', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ draft_id: activeDraftId })
        }).then(r => r.json());

        if (res.success) {
          showToast('Draft rejected and archived', 'info');
          globalDrafts = globalDrafts.filter(d => d.draft_id !== activeDraftId);
          activeDraftId = null;
          loadAllData();
        }
      } catch (e) {
        showToast('Error rejecting draft', 'error');
      }
    }

    async function batchApproveCTCReplies() {
      if (!confirm('Batch approve and send all standard CTC & 7-day notice period replies via SMTP?')) return;
      showToast('Executing batch approval...', 'info');

      try {
        const res = await fetch('/api/drafts/batch_approve', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ category: 'ctc_notice' })
        }).then(r => r.json());

        showToast(`Batch approved & sent ${res.approved_count} emails!`, 'success');
        loadAllData();
      } catch (e) {
        showToast('Error running batch approval', 'error');
      }
    }

    // RENDER TASKS LIST
    function renderTasksList() {
      const container = document.getElementById('tasksTableContainer');
      if (!container) return;

      const filtered = globalTasks.filter(t => {
        if (t.status !== 'PENDING') return false;
        if (currentTaskFilter === 'HIGH') return t.priority === 'HIGH';
        if (currentTaskFilter === 'SCHEDULE_CALL') return t.type === 'SCHEDULE_CALL';
        if (currentTaskFilter === 'APPLY_LINK_MANUAL') return t.type === 'APPLY_LINK_MANUAL';
        return true;
      });

      if (filtered.length === 0) {
        container.innerHTML = '<div class="p-8 text-center text-xs text-slate-400 font-medium">No pending tasks matching this filter.</div>';
        return;
      }

      let html = '<div class="divide-y divide-slate-100">';
      filtered.forEach(t => {
        const isCall = t.type === 'SCHEDULE_CALL';
        const isHigh = t.priority === 'HIGH';
        const pBadge = isCall ? 'bg-rose-50 text-rose-700 border-rose-200' :
                       isHigh ? 'bg-amber-50 text-amber-700 border-amber-200' : 'bg-slate-100 text-slate-600 border-slate-200';

        html += `
          <div class="py-3 flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-start space-x-3 min-w-0">
              <span class="text-base mt-0.5">${isCall ? '📞' : '📋'}</span>
              <div>
                <div class="flex items-center space-x-2">
                  <span class="text-xs font-bold text-slate-900">${t.title}</span>
                  <span class="text-[10px] px-1.5 py-0.2 rounded font-mono uppercase border ${pBadge} font-medium">${t.priority}</span>
                  <span class="text-[10px] font-mono text-slate-400">${t.id}</span>
                </div>
                <div class="text-[11px] text-slate-500 mt-0.5">
                  <strong class="text-slate-700">${t.company}</strong> &bull; ${t.recruiter_email || 'No email'} &bull; Due: ${t.due_date || 'ASAP'}
                </div>
              </div>
            </div>
            <div class="flex items-center space-x-2">
              ${t.action_url ? `<a href="${t.action_url}" target="_blank" class="text-xs text-indigo-700 hover:text-indigo-900 font-semibold px-2.5 py-1 bg-white border border-slate-200 rounded-lg shadow-xs">Open Link ↗</a>` : ''}
              <button onclick="completeTask('${t.id}')" class="text-xs bg-emerald-50 text-emerald-700 border border-emerald-200 hover:bg-emerald-100 font-semibold px-3 py-1 rounded-lg transition shadow-2xs">Complete</button>
              <button onclick="dismissTask('${t.id}')" class="text-xs text-slate-500 hover:text-rose-600 px-2 py-1 rounded font-medium">Dismiss</button>
            </div>
          </div>
        `;
      });
      html += '</div>';
      container.innerHTML = html;
    }

    function filterTasks(type) {
      currentTaskFilter = type;
      document.querySelectorAll('.task-filter-btn').forEach(b => {
        if (b.dataset.filter === type) {
          b.className = 'task-filter-btn px-2.5 py-1 rounded-lg bg-indigo-600 text-white font-semibold shadow-xs';
        } else {
          b.className = 'task-filter-btn px-2.5 py-1 rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200 font-medium';
        }
      });
      renderTasksList();
    }

    async function completeTask(taskId) {
      try {
        const res = await fetch('/api/tasks/complete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: taskId })
        }).then(r => r.json());

        if (res.success) {
          showToast(`Task ${taskId} marked as completed!`, 'success');
          globalTasks = globalTasks.filter(t => t.id !== taskId);
          loadAllData();
        }
      } catch (e) {
        showToast('Error completing task', 'error');
      }
    }

    async function dismissTask(taskId) {
      try {
        const res = await fetch('/api/tasks/dismiss', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: taskId })
        }).then(r => r.json());

        if (res.success) {
          showToast(`Task ${taskId} dismissed`, 'info');
          globalTasks = globalTasks.filter(t => t.id !== taskId);
          loadAllData();
        }
      } catch (e) {
        showToast('Error dismissing task', 'error');
      }
    }

    // RENDER CRM KANBAN
    function renderCrmKanban() {
      const q = (document.getElementById('crmSearchInput')?.value || '').toLowerCase();
      const filtered = globalCrm.filter(t => {
        if (!q) return true;
        return (t.company || '').toLowerCase().includes(q) ||
               (t.contact_email || '').toLowerCase().includes(q) ||
               (t.role || '').toLowerCase().includes(q);
      });

      const cols = {
        'OUTREACH_SENT': document.getElementById('crmColSent'),
        'REPLIED': document.getElementById('crmColReplied'),
        'LINK_RECEIVED': document.getElementById('crmColLink'),
        'SCREENING_REQUESTED': document.getElementById('crmColScreening'),
        'OFFER': document.getElementById('crmColOffer')
      };

      const counts = { 'OUTREACH_SENT': 0, 'REPLIED': 0, 'LINK_RECEIVED': 0, 'SCREENING_REQUESTED': 0, 'OFFER': 0 };

      Object.values(cols).forEach(c => { if (c) c.innerHTML = ''; });

      filtered.forEach(t => {
        let stage = t.stage || 'OUTREACH_SENT';
        if (stage === 'INTERVIEW_SCHEDULED' || stage === 'TECH_ASSESSMENT') stage = 'OFFER';
        if (!cols[stage]) stage = 'OUTREACH_SENT';

        counts[stage] = (counts[stage] || 0) + 1;

        const card = document.createElement('div');
        card.className = 'p-3 rounded-lg bg-white border border-slate-200/90 hover:border-slate-300 transition cursor-pointer shadow-xs hover:shadow-sm';
        card.onclick = () => alert(`Company: ${t.company}
Email: ${t.contact_email}
Role: ${t.role}
Stage: ${t.stage}
Last Interaction: ${t.last_interaction}`);

        card.innerHTML = `
          <div class="flex items-center justify-between mb-1">
            <strong class="text-xs font-bold text-slate-900 truncate max-w-[140px]">${t.company || 'Direct'}</strong>
            <span class="text-[9px] px-1.5 py-0.2 rounded font-mono uppercase bg-slate-100 text-slate-600 font-medium">${t.source_channel || 'DIRECT'}</span>
          </div>
          <div class="text-[11px] text-slate-600 truncate font-medium">${t.role || 'Full Stack Developer'}</div>
          <div class="text-[10px] text-slate-400 mt-1 flex items-center justify-between">
            <span class="truncate max-w-[120px]">${t.recruiter_name || t.contact_email}</span>
            <span>${(t.last_interaction || '').substring(5, 10)}</span>
          </div>
        `;
        if (cols[stage]) cols[stage].appendChild(card);
      });

      document.getElementById('crmColCountSent').innerText = counts['OUTREACH_SENT'];
      document.getElementById('crmColCountReplied').innerText = counts['REPLIED'];
      document.getElementById('crmColCountLink').innerText = counts['LINK_RECEIVED'];
      document.getElementById('crmColCountScreening').innerText = counts['SCREENING_REQUESTED'];
      document.getElementById('crmColCountOffer').innerText = counts['OFFER'];
    }

    // RENDER PORTALS GRID
    function renderPortalsGrid() {
      const container = document.getElementById('portalsGridContainer');
      if (!container || !globalState) return;

      const portals = globalState.portals || [];
      let html = '';

      portals.forEach(p => {
        const pct = Math.min(100, Math.round((p.applied_today / p.daily_limit) * 100));
        const isHealthy = p.is_healthy;
        const statusBadge = isHealthy ?
          '<span class="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-medium bg-emerald-50 text-emerald-700 border border-emerald-200"><span class="w-1.5 h-1.5 mr-1 rounded-full bg-emerald-500"></span>' + p.session_status + '</span>' :
          '<span class="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-medium bg-rose-50 text-rose-700 border border-rose-200"><span class="w-1.5 h-1.5 mr-1 rounded-full bg-rose-500"></span>' + p.session_status + '</span>';

        html += `
          <div class="bg-white border border-slate-200/90 rounded-xl p-4 flex flex-col justify-between space-y-3 shadow-xs hover:shadow transition">
            <div>
              <div class="flex items-center justify-between">
                <div class="flex items-center space-x-2">
                  <span class="text-xl">${p.icon}</span>
                  <div>
                    <h3 class="font-bold text-slate-900 text-xs">${p.name}</h3>
                    <span class="text-[10px] text-slate-400 font-medium">${p.category}</span>
                  </div>
                </div>
                ${statusBadge}
              </div>

              <!-- Progress Bar -->
              <div class="mt-3">
                <div class="flex items-center justify-between text-[11px] mb-1">
                  <span class="text-slate-500">Applies Today:</span>
                  <span class="font-bold text-slate-900">${p.applied_today} / ${p.daily_limit} <span class="text-slate-400 font-normal">(${pct}%)</span></span>
                </div>
                <div class="w-full bg-slate-100 h-1.5 rounded-full overflow-hidden border border-slate-200/40">
                  <div class="bg-indigo-600 h-full rounded-full transition-all duration-500" style="width: ${pct}%"></div>
                </div>
              </div>

              <div class="mt-2.5 text-[10px] text-slate-500 flex items-center justify-between">
                <span>Refreshed: <strong class="text-slate-700">${p.last_refresh}</strong></span>
                <span>Tracked: <strong class="text-slate-700">${p.total_jobs}</strong></span>
              </div>
            </div>

            <div class="flex items-center space-x-2 pt-2 border-t border-slate-100">
              <button onclick="triggerQuickAction('apply_${p.key}')" class="flex-1 bg-slate-50 hover:bg-slate-100 text-slate-700 border border-slate-200 text-[11px] font-semibold py-1.5 rounded-lg transition text-center shadow-2xs">
                Apply Now
              </button>
              ${p.supports_refresh ? `
                <button onclick="triggerQuickAction('refresh_${p.key}')" class="bg-slate-50 hover:bg-slate-100 text-slate-700 border border-slate-200 text-[11px] font-medium px-2.5 py-1.5 rounded-lg transition shadow-2xs" title="Refresh Profile">
                  🔄
                </button>
              ` : ''}
              ${!isHealthy ? `
                <button onclick="triggerQuickAction('login_${p.key}')" class="bg-amber-600 hover:bg-amber-700 text-white text-[11px] font-semibold px-2.5 py-1.5 rounded-lg transition shadow-2xs">
                  Login
                </button>
              ` : ''}
            </div>
          </div>
        `;
      });

      container.innerHTML = html;
    }

    // FETCH & RENDER SCREENSHOTS
    async function fetchScreenshots() {
      const container = document.getElementById('screenshotsGridContainer');
      const badge = document.getElementById('debuggerCountBadge');
      const tabBadge = document.getElementById('tabBadgeScreenshots');
      if (!container) return;

      try {
        const res = await fetch('/api/screenshots').then(r => r.json());
        const list = res.screenshots || [];
        if (badge) badge.innerText = `${list.length} captures`;
        if (tabBadge) tabBadge.innerText = list.length;

        if (list.length === 0) {
          container.innerHTML = '<div class="col-span-full p-8 text-center text-xs text-slate-400 font-medium">No obstacle screenshots found in logs/. All bots running smoothly.</div>';
          return;
        }

        let html = '';
        list.forEach(s => {
          const isStuck = s.is_stuck;
          const badgeClass = isStuck ? 'bg-rose-50 text-rose-700 border-rose-200' : 'bg-slate-100 text-slate-600 border-slate-200';

          html += `
            <div class="bg-white border border-slate-200/90 rounded-xl overflow-hidden flex flex-col justify-between shadow-xs hover:shadow transition">
              <div class="p-3 border-b border-slate-100 flex items-center justify-between">
                <div>
                  <strong class="text-xs text-slate-900 block truncate max-w-[180px]">${s.company}</strong>
                  <span class="text-[10px] text-slate-400">${s.modified_at} &bull; ${s.size_kb} KB</span>
                </div>
                <span class="text-[9px] px-1.5 py-0.2 rounded font-mono uppercase border ${badgeClass} font-semibold">${s.status_tag}</span>
              </div>
              <div class="h-44 bg-slate-50 overflow-hidden cursor-pointer relative group flex items-center justify-center p-2" onclick="openLightbox('/api/screenshot/${encodeURIComponent(s.filename)}', '${s.company}')">
                <img src="/api/screenshot/${encodeURIComponent(s.filename)}" alt="${s.filename}" class="max-h-full max-w-full object-contain rounded transition group-hover:scale-105 duration-300">
                <div class="absolute inset-0 bg-indigo-900/10 opacity-0 group-hover:opacity-100 transition flex items-center justify-center text-xs font-semibold text-indigo-900 bg-white/40 backdrop-blur-2xs">
                  Click to Expand ↗
                </div>
              </div>
              <div class="p-2.5 bg-slate-50/70 border-t border-slate-100 flex items-center justify-between">
                <button onclick="openLightbox('/api/screenshot/${encodeURIComponent(s.filename)}', '${s.company}')" class="text-[11px] text-indigo-700 hover:text-indigo-900 font-semibold">Zoom View</button>
                <button onclick="triggerQuickAction('login_workday')" class="text-[11px] bg-white hover:bg-slate-100 text-slate-700 border border-slate-200 px-2.5 py-1 rounded-lg font-medium shadow-2xs">Retry Headed</button>
              </div>
            </div>
          `;
        });
        container.innerHTML = html;
      } catch (e) {
        console.error('Error fetching screenshots:', e);
      }
    }

    function openLightbox(imgSrc, title) {
      document.getElementById('lightboxImg').src = imgSrc;
      document.getElementById('lightboxTitle').innerText = title || 'Screenshot Inspection';
      document.getElementById('imageLightboxModal').classList.remove('hidden');
    }

    function closeLightbox() {
      document.getElementById('imageLightboxModal').classList.add('hidden');
      document.getElementById('lightboxImg').src = '';
    }

    // LIVE LOGS
    async function fetchLiveLogs() {
      const out = document.getElementById('terminalLogOutput');
      if (!out) return;

      try {
        const res = await fetch('/api/logs').then(r => r.json());
        const lines = res.logs || [];
        if (lines.length === 0) {
          out.innerHTML = '<div class="text-slate-500">No logs found in logs/automation.log.</div>';
          return;
        }

        let html = '';
        lines.forEach(l => {
          let color = 'text-slate-300';
          if (l.includes('[ERROR]')) color = 'text-rose-400 font-semibold';
          else if (l.includes('[WARNING]')) color = 'text-amber-400 font-medium';
          else if (l.includes('🤝') || l.includes('Wave') || l.includes('Submitted')) color = 'text-emerald-400 font-medium';
          html += `<div class="${color}">${escapeHtml(l)}</div>`;
        });
        out.innerHTML = html;
        out.scrollTop = out.scrollHeight;
      } catch (e) {
        out.innerHTML = '<div class="text-rose-400">Error loading live logs.</div>';
      }
    }

    function escapeHtml(str) {
      return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    // TRIGGER QUICK ACTION
    async function triggerQuickAction(actionKey) {
      showToast(`Triggering action: ${actionKey}...`, 'info');
      try {
        const res = await fetch('/api/actions/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: actionKey })
        }).then(r => r.json());

        if (res.success) {
          showToast(res.message, 'success');
          loadAllData();
        } else {
          showToast(res.message || 'Action could not be started', 'error');
        }
      } catch (e) {
        showToast('Error sending action command', 'error');
      }
    }

    // AUTO-POLL EVERY 5 SECONDS (Real-time live updates)
    document.addEventListener('DOMContentLoaded', () => {
      loadAllData();
      setInterval(() => {
        loadAllData(false);
      }, 5000);
    });
  </script>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# HTTP Request Handler
# -----------------------------------------------------------------------------

class DashboardHTTPHandler(BaseHTTPRequestHandler):
    """Multi-threaded REST and Static Handler for Mission Control Web Dashboard."""

    def log_message(self, format, *args):
        # Silence routine request polling logs in console
        return

    def _send_json(self, status_code: int, data: dict):
        """Send JSON response with proper headers."""
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, HEAD')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 1. Main Dashboard HTML
        if path == '/' or path == '/index.html':
            template_path = os.path.join(BASE_DIR, 'templates', 'dashboard.html')
            if os.path.exists(template_path):
                with open(template_path, 'rb') as f:
                    html_bytes = f.read()
            else:
                html_bytes = WEB_HTML_TEMPLATE.encode('utf-8')

            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html_bytes)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
            self.wfile.write(html_bytes)

        # 2. Consolidated System State API
        elif path == '/api/state':
            state = get_system_state()
            self._send_json(200, state)

        # 3. Tasks List API
        elif path == '/api/tasks':
            data = task_manager.load_tasks()
            tasks_list = list(data.get("tasks", {}).values())
            # Sort by priority (HIGH first), then created_at desc
            tasks_list.sort(key=lambda x: (x.get("priority") != "HIGH", x.get("created_at", "")), reverse=False)
            self._send_json(200, {"tasks": tasks_list, "total": len(tasks_list)})

        # 4. Inbound Email Drafts API
        elif path == '/api/drafts':
            drafts_data = reply_agent.load_drafts()
            drafts = [d for d in drafts_data.get("drafts", {}).values() if d.get("status") == "PENDING"]
            drafts.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            self._send_json(200, {"drafts": drafts, "total": len(drafts)})

        # 5. Recruiter CRM Threads API
        elif path == '/api/crm':
            crm_data = crm_tracker.load_crm()
            threads = list(crm_data.get("threads", {}).values())
            threads.sort(key=lambda x: x.get("last_interaction", ""), reverse=True)
            self._send_json(200, {"threads": threads, "total": len(threads)})

        # 6. Screenshots List API
        elif path == '/api/screenshots':
            files = glob.glob(os.path.join(LOGS_DIR, "*.png"))
            res = []
            for f in sorted(files, key=os.path.getmtime, reverse=True):
                bname = os.path.basename(f)
                comp = "Unknown Company"
                status_tag = "DEBUG"
                if "greenhouse" in bname:
                    parts = bname.replace("greenhouse_", "").split("_")
                    status_tag = "GREENHOUSE " + parts[0].upper()
                    if len(parts) > 1: comp = parts[1]
                elif "workday" in bname:
                    parts = bname.replace("workday_", "").split("_")
                    status_tag = "WORKDAY " + parts[0].upper()
                    if len(parts) > 1: comp = parts[1]
                elif "shine" in bname:
                    status_tag = "SHINE"
                    comp = "Shine Application"

                res.append({
                    "filename": bname,
                    "company": comp.replace("-", " "),
                    "status_tag": status_tag,
                    "is_stuck": "stuck" in bname,
                    "modified_at": datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M:%S"),
                    "size_kb": round(os.path.getsize(f) / 1024, 1)
                })
            self._send_json(200, {"screenshots": res})

        # 7. Raw Screenshot Image File Serving
        elif path.startswith('/api/screenshot/'):
            filename = urllib.parse.unquote(path.replace('/api/screenshot/', ''))
            safe_filename = os.path.basename(filename)
            file_path = os.path.join(LOGS_DIR, safe_filename)

            if os.path.exists(file_path) and safe_filename.endswith('.png'):
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(os.path.getsize(file_path)))
                self.end_headers()
                with open(file_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()

        # 8. Live Terminal Logs API
        elif path == '/api/logs':
            log_file = os.path.join(LOGS_DIR, 'automation.log')
            lines = []
            if os.path.exists(log_file):
                try:
                    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                        lines = [line.strip() for line in f.readlines()[-120:] if line.strip()]
                except Exception:
                    pass
            self._send_json(200, {"logs": lines})

        # 9. Background Action Status API
        elif path == '/api/actions/status':
            self._send_json(200, ACTION_RUNNER.get_status())

        # 10. Pending Screening Questions Review API
        elif path == '/api/qa/pending':
            self._send_json(200, {
                "questions": pending_qa.get_pending_questions(status="PENDING"),
                "stats": pending_qa.get_pending_stats()
            })

        # 11. Full Learned QA Memory Bank API
        elif path == '/api/qa/bank':
            self._send_json(200, qa_memory.load_qa_memory())

        # 12. PWA Manifest & App Icons
        elif path == '/manifest.json':
            manifest_path = os.path.join(BASE_DIR, 'assets', 'manifest.json')
            if os.path.exists(manifest_path):
                with open(manifest_path, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/manifest+json')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()

        elif path in ('/favicon.ico', '/favicon.png'):
            fav_path = os.path.join(BASE_DIR, 'assets', 'favicon.png')
            if os.path.exists(fav_path):
                with open(fav_path, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()

        elif path.startswith('/assets/'):
            asset_subpath = urllib.parse.unquote(path.replace('/assets/', ''))
            safe_filename = os.path.basename(asset_subpath)
            asset_path = os.path.join(BASE_DIR, 'assets', safe_filename)
            if os.path.exists(asset_path):
                ext = os.path.splitext(safe_filename)[1].lower()
                content_types = {
                    '.png': 'image/png',
                    '.jpg': 'image/jpeg',
                    '.jpeg': 'image/jpeg',
                    '.svg': 'image/svg+xml',
                    '.json': 'application/json',
                    '.ico': 'image/x-icon',
                    '.icns': 'application/octet-stream'
                }
                ctype = content_types.get(ext, 'application/octet-stream')
                with open(asset_path, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_len = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_len).decode('utf-8') if content_len > 0 else '{}'
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}

        # 1. Complete Task
        if path == '/api/tasks/complete':
            task_id = payload.get("task_id", "")
            note = payload.get("note", "Completed from Mission Control Dashboard.")
            success = task_manager.complete_task(task_id, note)
            self._send_json(200, {"success": success, "task_id": task_id})

        # 2. Dismiss Task
        elif path == '/api/tasks/dismiss':
            task_id = payload.get("task_id", "")
            success = task_manager.dismiss_task(task_id)
            self._send_json(200, {"success": success, "task_id": task_id})

        # 3. Approve and Dispatch Recruiter Reply Draft
        elif path == '/api/drafts/approve':
            draft_id = payload.get("draft_id", "")
            success = reply_agent.approve_and_send_draft(draft_id)
            self._send_json(200, {"success": success, "draft_id": draft_id})

        # 4. Batch Approve Standard CTC & Notice Drafts
        elif path == '/api/drafts/batch_approve':
            drafts_data = reply_agent.load_drafts()
            all_drafts = drafts_data.get("drafts", {})
            approved_count = 0
            failed_count = 0

            # Target standard CTC / Notice period confirmation drafts
            for did, d in list(all_drafts.items()):
                if d.get("status") == "PENDING":
                    intent = d.get("intent", "")
                    subj = d.get("subject", "").lower()
                    body_text = d.get("body", "").lower()

                    if (intent in ("CONFIRM_CTC", "EXPERIENCE_NOTICE", "GENERAL_INQUIRY") or
                        "notice" in subj or "notice" in body_text or "ctc" in subj or "ctc" in body_text):
                        if reply_agent.approve_and_send_draft(did):
                            approved_count += 1
                        else:
                            failed_count += 1

            self._send_json(200, {
                "success": True,
                "approved_count": approved_count,
                "failed_count": failed_count
            })

        # 5. Update Draft Content
        elif path == '/api/drafts/update':
            draft_id = payload.get("draft_id", "")
            drafts_data = reply_agent.load_drafts()
            draft = drafts_data.get("drafts", {}).get(draft_id)
            if draft:
                draft["subject"] = payload.get("subject", draft.get("subject"))
                draft["body"] = payload.get("body", draft.get("body"))
                reply_agent.save_drafts(drafts_data)
                self._send_json(200, {"success": True})
            else:
                self._send_json(404, {"success": False, "error": "Draft not found"})

        # 6. Reject / Archive Draft
        elif path == '/api/drafts/reject':
            draft_id = payload.get("draft_id", "")
            drafts_data = reply_agent.load_drafts()
            draft = drafts_data.get("drafts", {}).get(draft_id)
            if draft:
                draft["status"] = "DISMISSED"
                reply_agent.save_drafts(drafts_data)

                # Also dismiss corresponding task if linked
                tasks_data = task_manager.load_tasks()
                for tid, t in tasks_data.get("tasks", {}).items():
                    if t.get("metadata", {}).get("draft_id") == draft_id:
                        task_manager.dismiss_task(tid)

                self._send_json(200, {"success": True})
            else:
                self._send_json(404, {"success": False, "error": "Draft not found"})

        # 7. Update CRM Thread Stage
        elif path == '/api/crm/stage':
            email = payload.get("email", "")
            new_stage = payload.get("stage", "")
            note = payload.get("note", "Stage updated via Mission Control Dashboard.")
            success = crm_tracker.update_stage(email, new_stage, note)
            self._send_json(200, {"success": success})

        # 8. Trigger Background Action Command
        elif path == '/api/actions/run':
            action_key = payload.get("action", "")
            success, msg = ACTION_RUNNER.trigger_action(action_key, payload)
            self._send_json(200, {"success": success, "message": msg})

        # 9. Resolve Pending QA Question
        elif path == '/api/qa/resolve':
            qid = payload.get("question_id", "")
            ans = payload.get("answer", "")
            success = pending_qa.resolve_pending_question(qid, ans)
            self._send_json(200, {"success": success, "question_id": qid})

        # 10. Dismiss Pending QA Question
        elif path == '/api/qa/dismiss':
            qid = payload.get("question_id", "")
            success = pending_qa.dismiss_pending_question(qid)
            self._send_json(200, {"success": success, "question_id": qid})

        # 11. Save Custom QA Memory Rule
        elif path == '/api/qa/save':
            question = payload.get("question", "")
            answer = payload.get("answer", "")
            success = qa_memory.save_learned_answer(question, answer, platform="Mission Control Web")
            self._send_json(200, {"success": success})

        # 12. Delete Learned QA Entry
        elif path == '/api/qa/delete':
            key = payload.get("key", "")
            mem = qa_memory.load_qa_memory()
            deleted = False
            if key in mem.get("exact_questions", {}):
                del mem["exact_questions"][key]
                deleted = True
            if key in mem.get("patterns", {}):
                del mem["patterns"][key]
                deleted = True
            if deleted:
                qa_memory.save_qa_memory(mem)
            self._send_json(200, {"success": deleted})

        # 13. Clean On-Demand Server Shutdown (Frees RAM to 0 MB)
        elif path == '/api/server/shutdown':
            self._send_json(200, {
                "success": True,
                "message": "Mission Control server stopping cleanly. All memory released (0 MB RAM)."
            })
            def _shutdown():
                time.sleep(0.4)
                global GLOBAL_SERVER
                if GLOBAL_SERVER:
                    try:
                        GLOBAL_SERVER.server_close()
                    except Exception:
                        pass
                os._exit(0)
            threading.Thread(target=_shutdown, daemon=True).start()

        else:
            self.send_response(404)
            self.end_headers()


# -----------------------------------------------------------------------------
# Background Service & LaunchAgent Helpers (macOS)
# -----------------------------------------------------------------------------

LAUNCH_AGENT_LABEL = "com.amrit.jobautomation.dashboard"
LAUNCH_AGENT_DIR = os.path.expanduser("~/Library/LaunchAgents")
LAUNCH_AGENT_TARGET = os.path.join(LAUNCH_AGENT_DIR, f"{LAUNCH_AGENT_LABEL}.plist")
SOURCE_PLIST = os.path.join(BASE_DIR, f"{LAUNCH_AGENT_LABEL}.plist")


def is_dashboard_running(port=8765) -> bool:
    """Check if the dashboard server is actively responding on localhost:port."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        res = s.connect_ex(('127.0.0.1', port))
        s.close()
        return res == 0
    except Exception:
        return False


def open_dashboard_in_browser(port=8765):
    """Open the Mission Control Dashboard in the default web browser."""
    url = f"http://127.0.0.1:{port}"
    print(f"🌐 Opening Mission Control Dashboard at {url} in your default browser...")
    webbrowser.open(url)


def install_launchd_service(port=8765) -> bool:
    """Install and enable Mission Control as an always-on macOS LaunchAgent background service."""
    if not sys.platform.startswith('darwin'):
        print("❌ LaunchAgent services are only supported on macOS.")
        return False

    os.makedirs(LAUNCH_AGENT_DIR, exist_ok=True)

    python_bin = sys.executable or "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
    dashboard_script = os.path.join(BASE_DIR, "dashboard.py")
    log_file = os.path.join(LOGS_DIR, "dashboard_service.log")

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>{dashboard_script}</string>
        <string>--port</string>
        <string>{port}</string>
        <string>--no-browser</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{BASE_DIR}</string>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
"""
    try:
        with open(SOURCE_PLIST, 'w', encoding='utf-8') as f:
            f.write(plist_content)

        with open(LAUNCH_AGENT_TARGET, 'w', encoding='utf-8') as f:
            f.write(plist_content)

        print(f"✅ Created LaunchAgent configuration at: {LAUNCH_AGENT_TARGET}")

        subprocess.run(["launchctl", "unload", LAUNCH_AGENT_TARGET], capture_output=True)
        time.sleep(0.5)

        load_res = subprocess.run(["launchctl", "load", "-w", LAUNCH_AGENT_TARGET], capture_output=True, text=True)
        if load_res.returncode != 0:
            print(f"⚠️ Notice during launchctl load: {load_res.stderr.strip()}")

        time.sleep(1.0)
        if is_dashboard_running(port):
            print(f"🚀 Mission Control background service successfully ACTIVE on http://127.0.0.1:{port}!")
            print(f"📌 You can now open http://localhost:{port} directly in any browser without terminal.")
            return True
        else:
            print("⏳ Service loaded. It will activate shortly. Check logs at: logs/dashboard_service.log")
            return True
    except Exception as e:
        print(f"❌ Failed to install service: {e}")
        return False


def uninstall_launchd_service() -> bool:
    """Unload and remove the Mission Control macOS LaunchAgent background service."""
    if not sys.platform.startswith('darwin'):
        print("❌ LaunchAgent services are only supported on macOS.")
        return False

    try:
        if os.path.exists(LAUNCH_AGENT_TARGET):
            subprocess.run(["launchctl", "unload", "-w", LAUNCH_AGENT_TARGET], capture_output=True)
            os.remove(LAUNCH_AGENT_TARGET)
            print(f"✅ Unloaded and removed LaunchAgent plist: {LAUNCH_AGENT_TARGET}")
        else:
            print("ℹ️ No active LaunchAgent plist found in ~/Library/LaunchAgents.")

        print("🛑 Mission Control background service has been stopped and uninstalled.")
        return True
    except Exception as e:
        print(f"❌ Error uninstalling service: {e}")
        return False


def check_service_status(port=8765):
    """Inspect and print the health of the Mission Control background service."""
    print("\n" + "=" * 65)
    print("      🚀 MISSION CONTROL DASHBOARD SERVICE STATUS")
    print("=" * 65)

    is_running = is_dashboard_running(port)
    plist_installed = os.path.exists(LAUNCH_AGENT_TARGET)

    print(f"🌐 HTTP Port {port} Listening  : {'✅ YES (Active)' if is_running else '❌ NO (Offline)'}")
    print(f"⚙️  LaunchAgent Installed : {'✅ YES (~/Library/LaunchAgents)' if plist_installed else '❌ NO'}")

    if sys.platform.startswith('darwin'):
        try:
            res = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
            found_line = next((line for line in res.stdout.splitlines() if LAUNCH_AGENT_LABEL in line), None)
            if found_line:
                parts = found_line.split()
                pid = parts[0] if len(parts) > 0 and parts[0] != '-' else "Inactive"
                last_exit = parts[1] if len(parts) > 1 else "0"
                print(f"📊 launchd Daemon State   : Loaded (PID: {pid}, Last Exit Code: {last_exit})")
            else:
                print(f"📊 launchd Daemon State   : Not currently registered in launchctl")
        except Exception:
            pass

    log_path = os.path.join(LOGS_DIR, "dashboard_service.log")
    if os.path.exists(log_path):
        size_kb = round(os.path.getsize(log_path) / 1024, 1)
        print(f"📄 Service Log File       : {log_path} ({size_kb} KB)")

    print("=" * 65 + "\n")


# -----------------------------------------------------------------------------
# Server Launcher
# -----------------------------------------------------------------------------

def start_dashboard(port=8765, open_browser=True):
    """Launch JobAutomation Mission Control Web Server."""
    url = f"http://127.0.0.1:{port}"

    # If already running on this port, do not crash with Errno 48; just open browser
    if is_dashboard_running(port):
        print("\n" + "=" * 65)
        print("      🚀 JOB AUTOMATION MISSION CONTROL DASHBOARD")
        print("=" * 65)
        print(f"🌐 Web UI Address : {url}")
        print("ℹ️  Server Status  : Already running in background (LaunchAgent daemon)")
        print("✨ Action         : Opening browser directly...")
        print("=" * 65 + "\n")
        if open_browser:
            webbrowser.open(url)
        return

    server_address = ('127.0.0.1', port)
    try:
        httpd = ThreadingHTTPServer(server_address, DashboardHTTPHandler)
        global GLOBAL_SERVER
        GLOBAL_SERVER = httpd
    except OSError as e:
        print(f"\n❌ Could not bind server to {url}: {e}")
        if open_browser:
            webbrowser.open(url)
        return

    print("\n" + "=" * 65)
    print("      🚀 JOB AUTOMATION MISSION CONTROL DASHBOARD")
    print("=" * 65)
    print(f"🌐 Web UI Address : {url}")
    print(f"📊 Live Telemetry : Real-time REST API active")
    print("⌨️  Stop Server    : Press Ctrl+C in this terminal\n")

    if open_browser:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Mission Control Dashboard server...")
        httpd.server_close()


def main():
    parser = argparse.ArgumentParser(description="JobAutomation Mission Control Web Dashboard")
    parser.add_argument("--port", "-p", type=int, default=8765, help="Port to bind web server (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open default browser")
    parser.add_argument("--install-service", action="store_true", help="Install Mission Control as an always-on macOS LaunchAgent background service")
    parser.add_argument("--uninstall-service", action="store_true", help="Uninstall and stop the macOS LaunchAgent background service")
    parser.add_argument("--service-status", action="store_true", help="Check status of the background macOS LaunchAgent service")
    parser.add_argument("--open", action="store_true", help="Open the running dashboard in your default browser")
    args = parser.parse_args()

    if args.install_service:
        install_launchd_service(port=args.port)
        return

    if args.uninstall_service:
        uninstall_launchd_service()
        return

    if args.service_status:
        check_service_status(port=args.port)
        return

    if args.open:
        open_dashboard_in_browser(port=args.port)
        return

    start_dashboard(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
