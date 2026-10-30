#!/usr/bin/env python3
"""
Unified Job Automation Command Center
Orchestrates:
  1. Cold Email Dispatcher (agent.py)
  2. Naukri Daily Refresher & Auto-Applier (naukri_agent.py)
  3. Instahyre Daily Refresher & 1-Click Auto-Applier (instahyre_agent.py)
  4. LinkedIn Recruiter Post Harvester & 1-Click Easy Apply (linkedin_agent.py)
"""

import os
import sys
import json
import argparse
import subprocess
from datetime import datetime, timedelta

import agent as email_agent
import naukri_agent
import instahyre_agent
import linkedin_agent
import indeed_agent
import shine_agent
import wellfound_agent
import jobgether_agent
import foundit_agent
import optimizer
import cold_email_agent
import greenhouse_agent
import workday_agent
import forms_agent
from power_utils import MacKeepAwake
import notifier
import keyword_tracker
import quota_manager
import crm_tracker
import task_manager
import recruiter_copilot
import recruiter_link_applier
import reply_agent

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')


def show_dashboard_status():
    """Print a clean consolidated dashboard of today's automation metrics."""
    config = email_agent.load_config()
    logs_dir = os.path.expanduser(config.get('paths', {}).get('logs', '~/Documents/JobAutomation/logs'))
    today_str = datetime.now().strftime("%Y-%m-%d")

    quotas = quota_manager.get_all_quotas(today_str)
    wave_info = quotas["wave_info"]
    curr_wave_str = f"Wave {wave_info['current_wave_num']} of {wave_info['total_waves']}" if wave_info['current_wave_num'] > 0 else "Pre-Wave 1"

    print("\n" + "=" * 65)
    print(f"      JOB AUTOMATION SYSTEM STATUS — {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print("=" * 65)
    print(f"\n🎯 PACED APPLICATION WAVES & DAILY QUOTA")
    print(f"   • Overall Progress:     {quota_manager.format_progress_bar(quotas['overall_percent'], 10)} {quotas['overall_percent']}% ({quotas['total_applied']} / {quotas['total_limit']} Submitted)")
    print(f"   • Balance Remaining:    {quotas['total_remaining']} applications/actions left to hit daily goal")
    print(f"   • Paced Schedule:       {curr_wave_str} | Next Auto-Sweep: {wave_info['next_wave_str']}")

    # 1. Emails Status
    sent_path = os.path.join(logs_dir, "sent_emails.json")
    emails_today = 0
    total_emails = 0
    cold_sent_today = 0
    records = {}
    if os.path.exists(sent_path):
        try:
            with open(sent_path, 'r', encoding='utf-8') as f:
                records = json.load(f)
                total_emails = len(records)
                emails_today = sum(1 for v in records.values() if str(v.get("sent_at", "")).startswith(today_str))
                cold_sent_today = sum(
                    1 for v in records.values()
                    if str(v.get("sent_at", "")).startswith(today_str) and v.get("application_type") == "COLD_OUTREACH"
                )
        except Exception:
            pass

    cold_queue_path = os.path.join(logs_dir, "cold_email_queue.json")
    cold_queued_count = 0
    if os.path.exists(cold_queue_path):
        try:
            with open(cold_queue_path, 'r', encoding='utf-8') as f:
                cold_queued_count = len(json.load(f))
        except Exception:
            pass

    cold_limit = config.get("cold_email", {}).get("daily_send_limit", 25)
    print(f"\n📧 COLD EMAIL PIPELINE (agent.py & cold_email_agent.py)")
    print(f"   • Applications Sent Today: {emails_today} (Direct Cold Outreach: {cold_sent_today}/{cold_limit})")
    print(f"   • Queued Cold Leads:      {cold_queued_count} Leads Pending Dispatch")
    print(f"   • Total All-Time Sends:   {total_emails}")
    print(f"   • Dispatch Window:        10:00 AM – 6:00 PM IST (Active)")

    # 2. Naukri Status
    naukri_ledger_path = os.path.join(logs_dir, "naukri_applied_jobs.json")
    naukri_today = 0
    naukri_total = 0
    naukri_refresh = "Never"
    if os.path.exists(naukri_ledger_path):
        try:
            with open(naukri_ledger_path, 'r', encoding='utf-8') as f:
                n_data = json.load(f)
                naukri_refresh = n_data.get("last_profile_refresh_at") or "Never"
                n_jobs = n_data.get("applied_jobs", {})
                naukri_total = len(n_jobs)
                naukri_today = sum(
                    1 for v in n_jobs.values()
                    if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str)
                )
        except Exception:
            pass

    naukri_limit = config.get("naukri", {}).get("daily_apply_limit", 35)
    print(f"\n💼 NAUKRI.COM (naukri_agent.py)")
    print(f"   • Profile Last Refreshed: {naukri_refresh}")
    print(f"   • Applies Today:          {naukri_today} / {naukri_limit} (Daily Cap)")
    print(f"   • Total Tracked Jobs:     {naukri_total}")

    # 3. Instahyre Status
    insta_ledger_path = os.path.join(logs_dir, "instahyre_applied_jobs.json")
    insta_today = 0
    insta_total = 0
    insta_refresh = "Never"
    insta_verified = False
    if os.path.exists(insta_ledger_path):
        try:
            with open(insta_ledger_path, 'r', encoding='utf-8') as f:
                i_data = json.load(f)
                insta_refresh = i_data.get("last_profile_refresh_at") or "Never"
                insta_verified = i_data.get("login_verified", False)
                i_jobs = i_data.get("applied_jobs", {})
                insta_total = len(i_jobs)
                insta_today = sum(
                    1 for v in i_jobs.values()
                    if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str)
                )
        except Exception:
            pass

    insta_limit = config.get("instahyre", {}).get("daily_apply_limit", 35)
    print(f"\n⚡ INSTAHYRE (instahyre_agent.py)")
    print(f"   • Session Login Status:   {'Logged In' if insta_verified else 'Needs One-Time Login (run: python3 run_all.py --login-instahyre)'}")
    print(f"   • Profile Last Refreshed: {insta_refresh}")
    print(f"   • Applies Today:          {insta_today} / {insta_limit} (Daily Cap)")
    print(f"   • Total Tracked Opps:     {insta_total}")

    # 4. LinkedIn Status
    li_ledger_path = os.path.join(logs_dir, "linkedin_applied_jobs.json")
    li_today_applies = 0
    li_total_applies = 0
    li_today_harvest = 0
    li_total_harvest = 0
    li_verified = False
    if os.path.exists(li_ledger_path):
        try:
            with open(li_ledger_path, 'r', encoding='utf-8') as f:
                l_data = json.load(f)
                li_verified = l_data.get("login_verified", False)
                l_jobs = l_data.get("applied_jobs", {})
                li_total_applies = len(l_jobs)
                li_today_applies = sum(
                    1 for v in l_jobs.values()
                    if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str)
                )
                l_harvest = l_data.get("harvested_posts", {})
                li_total_harvest = len(l_harvest)
                li_today_harvest = sum(
                    1 for v in l_harvest.values()
                    if str(v.get("harvested_at", "")).startswith(today_str)
                )
        except Exception:
            pass

    li_limit = config.get("linkedin", {}).get("daily_apply_limit", 20)
    li_harvest_limit = config.get("linkedin", {}).get("daily_harvest_limit", 100)
    li_conn_cfg = config.get("linkedin", {}).get("connection_outreach", {})
    li_conn_daily_limit = li_conn_cfg.get("daily_limit", 12)
    li_conn_weekly_limit = li_conn_cfg.get("weekly_limit", 80)
    li_platform_max_weekly = li_conn_cfg.get("platform_max_weekly_limit", 100)
    li_hard_cap = li_conn_cfg.get("hard_network_cap", 30000)
    li_conn_path = os.path.join(logs_dir, "linkedin_connections.json")
    li_conn_today = 0
    li_conn_week = 0
    li_conn_total = 0
    is_restricted = False
    restricted_until = None
    if os.path.exists(li_conn_path):
        try:
            with open(li_conn_path, 'r', encoding='utf-8') as f:
                conn_data = json.load(f)
                conns = conn_data.get("connections", {})
                li_conn_total = len(conns)
                res_info = conn_data.get("platform_restriction", {})
                if res_info.get("is_restricted"):
                    res_u = res_info.get("restricted_until")
                    if res_u:
                        try:
                            if datetime.now() < datetime.strptime(res_u[:19], "%Y-%m-%d %H:%M:%S"):
                                is_restricted = True
                                restricted_until = res_u
                        except Exception:
                            pass
                for _, info in conns.items():
                    if isinstance(info, dict):
                        status = info.get("status", "")
                        ts_str = str(info.get("sent_at", ""))
                        if status in ("INVITED", "SENT"):
                            if ts_str.startswith(today_str):
                                li_conn_today += 1
                            try:
                                dt = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
                                if dt >= datetime.now() - timedelta(days=7):
                                    li_conn_week += 1
                            except Exception:
                                pass
        except Exception:
            pass

    print(f"\n🌐 LINKEDIN (linkedin_agent.py)")
    print(f"   • Session Login Status:   {'Logged In' if li_verified else 'Needs One-Time Login (run: python3 run_all.py --login-linkedin)'}")
    print(f"   • Recruiter Posts Mined:  {li_today_harvest} / {li_harvest_limit} Today (Daily Cap) | {li_total_harvest} Total (Auto-fed to Dropzone)")
    print(f"   • Easy Applies Today:     {li_today_applies} / {li_limit} (Daily Cap)")
    if is_restricted:
        print(f"   • Connection Outreach:    ⚠️ 7-Day Platform Lock Active until {restricted_until} (Halting to protect account)")
    else:
        print(f"   • Connection Outreach:    {li_conn_today} / {li_conn_daily_limit} Today | {li_conn_week} / {li_conn_weekly_limit} Last 7 Days (Safe Cap, Max {li_platform_max_weekly}/wk) | {li_conn_total} Total Tracked (Cap: {li_hard_cap:,})")
    print(f"   • Total Tracked Jobs:     {li_total_applies}")

    # 5. Indeed Status
    ind_ledger_path = os.path.join(logs_dir, "indeed_applied_jobs.json")
    ind_today = 0
    ind_total = 0
    ind_verified = False
    if os.path.exists(ind_ledger_path):
        try:
            with open(ind_ledger_path, 'r', encoding='utf-8') as f:
                ind_data = json.load(f)
                ind_verified = ind_data.get("login_verified", False)
                ind_jobs = ind_data.get("applied_jobs", {})
                ind_total = len(ind_jobs)
                ind_today = sum(
                    1 for v in ind_jobs.values()
                    if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str)
                )
        except Exception:
            pass

    ind_limit = config.get("indeed", {}).get("daily_apply_limit", 50)
    print(f"\n🔍 INDEED INDIA (indeed_agent.py)")
    print(f"   • Session Login Status:   {'Logged In' if ind_verified else 'Needs One-Time Login (run: python3 run_all.py --login-indeed)'}")
    print(f"   • Applies Today:          {ind_today} / {ind_limit} (Daily Cap)")
    print(f"   • Total Tracked Jobs:     {ind_total}")

    # 6. Shine Status
    sh_ledger_path = os.path.join(logs_dir, "shine_applied_jobs.json")
    sh_today = 0
    sh_total = 0
    sh_refresh = "Never"
    sh_verified = False
    if os.path.exists(sh_ledger_path):
        try:
            with open(sh_ledger_path, 'r', encoding='utf-8') as f:
                sh_data = json.load(f)
                sh_verified = sh_data.get("login_verified", False)
                sh_refresh = sh_data.get("last_profile_refresh_at") or "Never"
                sh_jobs = sh_data.get("applied_jobs", {})
                sh_total = len(sh_jobs)
                sh_today = sum(
                    1 for v in sh_jobs.values()
                    if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str)
                )
        except Exception:
            pass

    sh_limit = config.get("shine", {}).get("daily_apply_limit", 50)
    print(f"\n✨ SHINE.COM (shine_agent.py)")
    print(f"   • Session Login Status:   {'Logged In' if sh_verified else 'Needs One-Time Login (run: python3 run_all.py --login-shine)'}")
    print(f"   • Profile Last Refreshed: {sh_refresh}")
    print(f"   • Applies Today:          {sh_today} / {sh_limit} (Daily Cap)")
    print(f"   • Total Tracked Jobs:     {sh_total}")

    # 7. Wellfound Status
    wf_ledger_path = os.path.join(logs_dir, "wellfound_applied_jobs.json")
    wf_today = 0
    wf_total = 0
    wf_refresh = "Never"
    wf_verified = False
    if os.path.exists(wf_ledger_path):
        try:
            with open(wf_ledger_path, 'r', encoding='utf-8') as f:
                wf_data = json.load(f)
                wf_verified = wf_data.get("login_verified", False)
                wf_refresh = wf_data.get("last_profile_refresh_at") or "Never"
                wf_jobs = wf_data.get("applied_jobs", {})
                wf_total = len(wf_jobs)
                wf_today = sum(
                    1 for v in wf_jobs.values()
                    if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str)
                )
        except Exception:
            pass

    wf_limit = config.get("wellfound", {}).get("daily_apply_limit", 40)
    print(f"\n🚀 WELLFOUND / ANGELIST (wellfound_agent.py)")
    print(f"   • Session Login Status:   {'Logged In' if wf_verified else 'Needs One-Time Login (run: python3 run_all.py --login-wellfound)'}")
    print(f"   • Profile Last Refreshed: {wf_refresh}")
    print(f"   • Applies Today:          {wf_today} / {wf_limit} (Daily Cap)")
    print(f"   • Total Tracked Jobs:     {wf_total}")

    # 8. Jobgether Portal Status
    jg_ledger_path = os.path.join(logs_dir, "jobgether_applied_jobs.json")
    jg_verified = False
    jg_refresh = "Never"
    jg_total = 0
    jg_today = 0
    if os.path.exists(jg_ledger_path):
        try:
            with open(jg_ledger_path, 'r', encoding='utf-8') as f:
                jg_data = json.load(f)
                jg_verified = jg_data.get("login_verified", False)
                jg_refresh = jg_data.get("last_profile_refresh_at") or "Never"
                jg_jobs = jg_data.get("applied_jobs", {})
                jg_total = len(jg_jobs)
                jg_today = sum(
                    1 for v in jg_jobs.values()
                    if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str)
                )
        except Exception:
            pass

    jg_limit = config.get("jobgether", {}).get("daily_apply_limit", 50)
    print(f"\n🌍 JOBGETHER REMOTE (jobgether_agent.py)")
    print(f"   • Session Login Status:   {'Logged In' if jg_verified else 'Needs One-Time Login (run: python3 run_all.py --login-jobgether)'}")
    print(f"   • Profile Last Refreshed: {jg_refresh}")
    print(f"   • Applies Today:          {jg_today} / {jg_limit} (Daily Cap)")
    print(f"   • Total Tracked Jobs:     {jg_total}")

    # 9. Foundit (Monster India) Status
    fnd_ledger_path = os.path.join(logs_dir, "foundit_applied_jobs.json")
    fnd_verified = False
    fnd_refresh = "Never"
    fnd_total = 0
    fnd_today = 0
    if os.path.exists(fnd_ledger_path):
        try:
            with open(fnd_ledger_path, 'r', encoding='utf-8') as f:
                fnd_data = json.load(f)
                fnd_verified = fnd_data.get("login_verified", False)
                fnd_refresh = fnd_data.get("last_profile_refresh_at") or "Never"
                fnd_jobs = fnd_data.get("applied_jobs", {})
                fnd_total = len(fnd_jobs)
                fnd_today = sum(
                    1 for v in fnd_jobs.values()
                    if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str)
                )
        except Exception:
            pass

    fnd_limit = config.get("foundit", {}).get("daily_apply_limit", 50)
    print(f"\n🎯 FOUNDIT / MONSTER INDIA (foundit_agent.py)")
    print(f"   • Session Login Status:   {'Logged In' if fnd_verified else 'Needs One-Time Login (run: python3 run_all.py --login-foundit)'}")
    print(f"   • Profile Last Refreshed: {fnd_refresh}")
    print(f"   • Applies Today:          {fnd_today} / {fnd_limit} (Daily Cap)")
    print(f"   • Total Tracked Jobs:     {fnd_total}")

    # 10. Greenhouse ATS Status
    gh_ledger_path = os.path.join(logs_dir, "greenhouse_applied_jobs.json")
    gh_today = 0
    gh_total = 0
    gh_last_run = "Never"
    if os.path.exists(gh_ledger_path):
        try:
            with open(gh_ledger_path, 'r', encoding='utf-8') as f:
                gh_data = json.load(f)
                gh_last_run = gh_data.get("last_run_at") or "Never"
                gh_jobs = gh_data.get("applied_jobs", {})
                gh_total = len(gh_jobs)
                gh_today = sum(
                    1 for v in gh_jobs.values()
                    if v.get("status") in ("APPLIED", "SUBMITTED") and str(v.get("applied_at", "")).startswith(today_str)
                )
        except Exception:
            pass

    gh_limit = config.get("greenhouse", {}).get("daily_apply_limit", 25)
    gh_boards = len(config.get("greenhouse", {}).get("target_boards", []))
    print(f"\n🏢 GREENHOUSE ATS (greenhouse_agent.py)")
    print(f"   • Configured Boards:      {gh_boards} Tech Companies (Stripe, Databricks, Cloudflare, etc.)")
    print(f"   • Last Sweep Run:         {gh_last_run}")
    print(f"   • Applies Today:          {gh_today} / {gh_limit} (Daily Cap)")
    print(f"   • Total Tracked Jobs:     {gh_total}")

    # 11. Workday ATS Status
    wd_ledger_path = os.path.join(logs_dir, "workday_applied_jobs.json")
    wd_today = 0
    wd_total = 0
    wd_last_run = "Never"
    if os.path.exists(wd_ledger_path):
        try:
            with open(wd_ledger_path, 'r', encoding='utf-8') as f:
                wd_data = json.load(f)
                wd_last_run = wd_data.get("last_run_at") or "Never"
                wd_jobs = wd_data.get("applied_jobs", {})
                wd_total = len(wd_jobs)
                wd_today = sum(
                    1 for v in wd_jobs.values()
                    if v.get("status") in ("APPLIED", "SUBMITTED") and str(v.get("applied_at", "")).startswith(today_str)
                )
        except Exception:
            pass

    wd_limit = config.get("workday", {}).get("daily_apply_limit", 20)
    wd_portals = len(config.get("workday", {}).get("target_portals", []))
    print(f"\n💼 WORKDAY ATS (workday_agent.py)")
    print(f"   • Configured Portals:     {wd_portals} Enterprise Portals (Autodesk, Adobe, Nvidia, Salesforce, etc.)")
    print(f"   • Last Sweep Run:         {wd_last_run}")
    print(f"   • Applies Today:          {wd_today} / {wd_limit} (Daily Cap)")
    print(f"   • Total Tracked Jobs:     {wd_total}")

    # 12. Google & Microsoft Forms Status
    forms_ledger_path = os.path.join(logs_dir, "forms_applied.json")
    forms_today = 0
    forms_total = 0
    forms_last_run = "Never"
    if os.path.exists(forms_ledger_path):
        try:
            with open(forms_ledger_path, 'r', encoding='utf-8') as f:
                f_data = json.load(f)
                forms_last_run = f_data.get("last_run_at") or "Never"
                f_jobs = f_data.get("applied_forms", {})
                forms_total = len(f_jobs)
                forms_today = sum(
                    1 for v in f_jobs.values()
                    if v.get("status") in ("APPLIED", "SUBMITTED") and str(v.get("applied_at", "")).startswith(today_str)
                )
        except Exception:
            pass

    forms_limit = config.get("forms", {}).get("daily_apply_limit", 25)
    print(f"\n📋 GOOGLE & MICROSOFT FORMS (forms_agent.py)")
    print(f"   • Engines:                Google Forms (forms.gle) & Microsoft Forms (office.com)")
    print(f"   • Last Sweep Run:         {forms_last_run}")
    print(f"   • Applies Today:          {forms_today} / {forms_limit} (Daily Cap)")
    print(f"   • Total Tracked Forms:    {forms_total}")

    # 13. Recruiter CRM & Action Items Status
    crm_summary = crm_tracker.get_summary()
    pending_tasks = task_manager.get_pending_tasks()
    drafts_data = reply_agent.load_drafts()
    pending_drafts = [d for d in drafts_data.get("drafts", {}).values() if d.get("status") == "PENDING"]
    print(f"\n🤝 RECRUITER RELATIONSHIPS & CRM PIPELINE (recruiter_copilot.py)")
    print(f"   • Tracked Contacts:       {crm_summary['total_contacts']} total ({crm_summary['active_engagements']} active engagements)")
    print(f"   • Pending Action Tasks:   {len(pending_tasks)} Action Items (Run: python3 run_all.py --tasks)")
    print(f"   • Pending Reply Drafts:   {len(pending_drafts)} Ready for Review (Run: python3 reply_agent.py)")
    print(f"   • Inbound Auto-Sweep:     Run: python3 run_all.py --inbound")

    # 14. Autonomous Self-Learning & Intelligence Status
    winning_path = os.path.join(logs_dir, "winning_examples.json")
    winning_count = 0
    if os.path.exists(winning_path):
        try:
            with open(winning_path, 'r', encoding='utf-8') as f:
                winning_count = len(json.load(f))
        except Exception:
            pass

    kb_path = os.path.join(logs_dir, "screenshot_knowledge_base.json")
    kb_count = 0
    if os.path.exists(kb_path):
        try:
            with open(kb_path, 'r', encoding='utf-8') as f:
                kb_count = len(json.load(f).get("history", []))
        except Exception:
            pass

    profile_path = os.path.join(logs_dir, "user_intent_profile.json")
    top_persona = "Java Microservices (50%)"
    top_skills_preview = "Java 21, Spring Boot 3, Microservices, Kafka"
    if os.path.exists(profile_path):
        try:
            with open(profile_path, 'r', encoding='utf-8') as f:
                p_data = json.load(f)
                personas = p_data.get("personas", [])
                if personas:
                    top_persona = f"{personas[0].get('name')} ({int(personas[0].get('weight', 0.5)*100)}%)"
                w_skills = list(p_data.get("weighted_skills", {}).keys())
                if w_skills:
                    top_skills_preview = ", ".join(w_skills[:4])
        except Exception:
            pass

    hunt_path = os.path.join(logs_dir, "lead_hunt_queue.json")
    hunt_pending = 0
    if os.path.exists(hunt_path):
        try:
            with open(hunt_path, 'r', encoding='utf-8') as f:
                h_targets = json.load(f).get("targets", [])
                hunt_pending = sum(1 for t in h_targets if t.get("status") == "PENDING_HUNT")
        except Exception:
            pass

    print(f"\n🧠 AUTONOMOUS INTENT LEARNING & INTELLIGENCE")
    print(f"   • Screenshots Ingested:    {kb_count} Analyzed (Intent Knowledge Base)")
    print(f"   • Active Target Persona:   {top_persona}")
    print(f"   • Top Screenshot Skills:   {top_skills_preview}")
    print(f"   • Lead Hunt Queue:         {hunt_pending} Pending Targets (No-Email Screenshots)")
    print(f"   • Few-Shot Winning Memory: {winning_count} Real Recruiter Responses In-Context")
    print(f"   • Master Intelligence:     logs/SYSTEM_INTELLIGENCE.md (Run: python3 run_all.py --optimize)")

    # 6. Background Automation Scheduler Status (macOS launchd)
    daily_service_loaded = False
    dropzone_service_loaded = False
    bot_service_loaded = False
    try:
        res = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            daily_service_loaded = "com.amrit.jobautomation.daily" in res.stdout
            dropzone_service_loaded = "com.amrit.jobautomation" in res.stdout
            bot_service_loaded = "com.amrit.jobautomation.bot" in res.stdout
    except Exception:
        pass

    daily_plist = os.path.expanduser("~/Library/LaunchAgents/com.amrit.jobautomation.daily.plist")
    dropzone_plist = os.path.expanduser("~/Library/LaunchAgents/com.amrit.jobautomation.plist")
    bot_plist = os.path.expanduser("~/Library/LaunchAgents/com.amrit.jobautomation.bot.plist")
    if not daily_service_loaded and os.path.exists(daily_plist):
        daily_service_loaded = True
    if not dropzone_service_loaded and os.path.exists(dropzone_plist):
        dropzone_service_loaded = True
    if not bot_service_loaded and os.path.exists(bot_plist):
        bot_service_loaded = True

    print(f"\n⏰ AUTOMATED BACKGROUND SCHEDULER (macOS launchd)")
    print(f"   • 5-Wave Daily Automation (9:30, 11:30, 13:30, 15:30, 17:30): {'Active (com.amrit.jobautomation.daily)' if daily_service_loaded else 'Not Installed'}")
    print(f"   • Dropzone Email Watcher:             {'Active - Every 5 mins (com.amrit.jobautomation)' if dropzone_service_loaded else 'Not Installed'}")
    print(f"   • Telegram 24/7 Command Listener:     {'Active (com.amrit.jobautomation.bot)' if bot_service_loaded else 'Not Installed'}")

    # 7. Notification & Telegram Bot Status
    tg_notif = notifier.TelegramNotifier()
    tg_status = f"Connected (Chat ID: {tg_notif.chat_id})" if tg_notif.is_configured() else "Disabled / Pending Token in config.json"
    wa_notif = notifier.WhatsAppNotifier()
    wa_status = f"Active ({wa_notif.phone})" if wa_notif.is_configured() else "Disabled"

    print(f"\n📲 LIVE NOTIFICATIONS & HUMAN-IN-THE-LOOP BOT")
    print(f"   • Telegram Bot:            {tg_status}")
    print(f"   • WhatsApp Alerts:         {wa_status}")
    print(f"   • macOS Alert Banners:     Active (Urgent/Stuck overrides quiet_mode)")
    print(f"   • Test Dispatch:           Run: python3 notifier.py --test")
    print(f"   • Interactive Bot Daemon:  Run: python3 run_all.py --bot")

    # 8. Keyword Yield & Performance Summary
    kw_summary = keyword_tracker.get_daily_keyword_summary(today_str)
    print(f"\n🎯 KEYWORD SEARCH & YIELD INTELLIGENCE (keyword_tracker.py)")
    print(f"   • Total Queries Executed:  {kw_summary['total_queries_run']} runs today")
    print(f"   • Scanned vs Applied:      {kw_summary['total_scanned']} scanned -> {kw_summary['total_applied']} submitted")
    if kw_summary["discovered_trending"]:
        print(f"   • Discovered Market Roles: {', '.join(kw_summary['discovered_trending'][:4])}")
    print(f"   • Full Report:             Run: python3 run_all.py --notify-keywords")

    print("\n" + "=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Unified Job Automation Command Center")
    parser.add_argument("--status", action="store_true", help="Display live consolidated metrics dashboard.")
    parser.add_argument("--dashboard", action="store_true", help="Launch JobAutomation Mission Control Web Dashboard in browser.")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind Mission Control Web Dashboard (default: 8765).")
    parser.add_argument("--optimize", action="store_true", help="Run autonomous self-learning optimization cycle.")
    parser.add_argument("--login-naukri", action="store_true", help="Launch interactive login for Naukri.")
    parser.add_argument("--login-instahyre", action="store_true", help="Launch interactive login for Instahyre.")
    parser.add_argument("--login-linkedin", action="store_true", help="Launch interactive login for LinkedIn.")
    parser.add_argument("--login-indeed", action="store_true", help="Launch interactive login for Indeed India.")
    parser.add_argument("--login-shine", action="store_true", help="Launch interactive login for Shine.com.")
    parser.add_argument("--login-wellfound", action="store_true", help="Launch interactive login for Wellfound.")
    parser.add_argument("--login-jobgether", action="store_true", help="Launch interactive login for Jobgether Remote.")
    parser.add_argument("--login-foundit", action="store_true", help="Launch interactive login for Foundit (Monster India).")
    parser.add_argument("--login-forms", action="store_true", help="Launch interactive login for Google / Microsoft Forms browser profile.")
    parser.add_argument("--login-all", action="store_true", help="Launch interactive login for all portals.")
    parser.add_argument("--refresh-all", action="store_true", help="Run profile refresh across all refresh-capable portals.")
    parser.add_argument("--refresh-shine", action="store_true", help="Run profile refresh on Shine.com.")
    parser.add_argument("--refresh-wellfound", action="store_true", help="Run candidate check-in on Wellfound.")
    parser.add_argument("--refresh-jobgether", action="store_true", help="Run profile refresh on Jobgether.")
    parser.add_argument("--refresh-foundit", action="store_true", help="Run profile refresh on Foundit.")
    parser.add_argument("--apply-naukri", action="store_true", help="Run auto-apply on Naukri.")
    parser.add_argument("--apply-instahyre", action="store_true", help="Run auto-apply on Instahyre.")
    parser.add_argument("--apply-linkedin", action="store_true", help="Run 1-Click Easy Apply bot on LinkedIn.")
    parser.add_argument("--apply-indeed", action="store_true", help="Run auto-apply on Indeed India.")
    parser.add_argument("--apply-shine", action="store_true", help="Run auto-apply on Shine.com.")
    parser.add_argument("--apply-wellfound", action="store_true", help="Run auto-apply on Wellfound.")
    parser.add_argument("--apply-jobgether", action="store_true", help="Run auto-apply on Jobgether Remote.")
    parser.add_argument("--apply-foundit", action="store_true", help="Run auto-apply on Foundit (Monster India).")
    parser.add_argument("--apply-greenhouse", "--greenhouse", action="store_true", help="Run auto-apply & board sweep on Greenhouse ATS.")
    parser.add_argument("--greenhouse-url", type=str, help="Directly apply to a specific Greenhouse job URL.")
    parser.add_argument("--apply-workday", "--workday", action="store_true", help="Run auto-apply & portal sweep on Workday ATS.")
    parser.add_argument("--workday-url", type=str, help="Directly apply to a specific Workday job URL.")
    parser.add_argument("--apply-forms", "--forms", action="store_true", help="Process pending Google & Microsoft Forms tickets.")
    parser.add_argument("--form-url", type=str, help="Directly apply to a specific Google Form or Microsoft Form URL.")
    parser.add_argument("--review", action="store_true", help="Fill application form and pause for manual review before submit.")
    parser.add_argument("--harvest-linkedin", action="store_true", help="Mine LinkedIn hiring posts with emails in past 24h.")
    parser.add_argument("--connect-linkedin", action="store_true", help="Send connection requests to verified hiring managers & agency recruiters.")
    parser.add_argument("--apply-all", action="store_true", help="Run auto-apply across all portals & ATS systems (Naukri, Instahyre, LinkedIn, Indeed, Shine, Wellfound, Jobgether, Foundit, Greenhouse, Workday, Google/MS Forms).")
    parser.add_argument("--emails", action="store_true", help="Trigger email dropzone processor.")
    parser.add_argument("--cold-email", nargs="?", const="DISPATCH", default=None,
                        help="Ingest and dispatch direct cold outreach emails. Accepts optional image, text file, or email string. If no value provided, dispatches queued leads.")
    parser.add_argument("--dry-run", action="store_true", help="Preview cold outreach emails without sending.")
    parser.add_argument("--all", action="store_true", help="Run complete daily cycle across all portals (including LinkedIn harvest & apply) and email.")
    parser.add_argument("--headed", action="store_true", help="Show visible Chrome browser window (default is headless/invisible).")
    parser.add_argument("--headless", action="store_true", help="Force headless Chrome browser execution (invisible background).")
    parser.add_argument("--offscreen", action="store_true", help="Run headed Chrome off-screen to avoid covering workspace.")
    parser.add_argument("--no-screen", action="store_true", help="Keep Mac awake but allow physical screen to sleep.")
    parser.add_argument("--quiet", action="store_true", help="Suppress routine per-job notifications and show only final summary.")
    parser.add_argument("--quota", action="store_true", help="Display live application quota progress bars & remaining targets.")
    parser.add_argument("--inbound", "--recruiter-sweep", action="store_true", help="Run full inbound recruiter email harvest, link auto-apply, and reply drafting.")
    parser.add_argument("--crm", "--track", action="store_true", help="Display interactive Recruiter Relationship CRM pipeline table.")
    parser.add_argument("--tasks", action="store_true", help="Display candidate action items and follow-up tasks ledger.")
    parser.add_argument("--complete-task", type=str, help="Mark a candidate task as completed by task ID.")
    parser.add_argument("--apply-recruiter-link", type=str, help="Autonomously solve and apply to a recruiter-provided application link.")
    parser.add_argument("--approve-reply", type=str, help="Approve and send a pending recruiter reply draft by draft ID.")
    parser.add_argument("--notify-digest", action="store_true", help="Format and dispatch consolidated daily status digest to Telegram / WhatsApp.")
    parser.add_argument("--notify-quota", action="store_true", help="Format and dispatch live quota progress bars report to Telegram.")
    parser.add_argument("--notify-keywords", action="store_true", help="Format and dispatch keyword performance & yield analytics report.")
    parser.add_argument("--health", "--diagnostics", action="store_true", help="Display live platform bottleneck diagnostics, root cause analysis & overcoming guide.")
    parser.add_argument("--weekly-status", "--weekly", action="store_true", help="Display 7-day comparative weekly status and platform performance audit.")
    parser.add_argument("--notify-health", action="store_true", help="Dispatch live bottleneck diagnostics & overcoming guide to Telegram.")
    parser.add_argument("--notify-weekly", action="store_true", help="Dispatch 7-day comparative weekly status audit to Telegram.")
    parser.add_argument("--bot", action="store_true", help="Launch interactive background Telegram command listener daemon.")
    args = parser.parse_args()

    if args.dashboard:
        import dashboard
        dashboard.start_dashboard(port=args.port, open_browser=True)
        return

    if len(sys.argv) == 1 or args.status:
        show_dashboard_status()
        return

    if args.crm:
        crm_tracker.print_crm_table()
        return

    if args.tasks:
        task_manager.print_tasks_table(include_completed=False)
        return

    if args.complete_task:
        task_manager.complete_task(args.complete_task, completion_note="Completed via CLI")
        print(f"✅ Marked task {args.complete_task} as COMPLETED.")
        return

    if args.approve_reply:
        success = reply_agent.approve_and_send_draft(args.approve_reply)
        print(f"Draft {args.approve_reply} status: {'SENT VIA SMTP' if success else 'FAILED'}")
        return

    if args.apply_recruiter_link:
        print(f"\n[Recruiter Link Applier] Autonomously applying to: {args.apply_recruiter_link}...")
        res = recruiter_link_applier.apply_to_recruiter_link(
            url=args.apply_recruiter_link,
            dry_run=args.dry_run,
            force_headed=args.headed,
            review_mode=args.review
        )
        print(f"Result: {'SUCCESS' if res['success'] else 'NEEDS MANUAL / PENDING'}")
        print(f"Notes: {res['notes']}")
        return

    if args.inbound:
        print("\n[Unified Launcher] Starting Inbound Recruiter Sweep...")
        res = recruiter_copilot.run_recruiter_copilot_cycle(dry_run=args.dry_run, force_headed=args.headed)
        print(f"Sweep Completed: {res.get('new_messages_ingested', 0)} new messages, {res.get('links_successful', 0)} links applied, {res.get('reply_drafts_prepared', 0)} drafts prepared.")
        return

    if args.quota:
        quota_manager.print_cli_summary()
        return

    if args.bot:
        notifier.run_telegram_bot_listener()
        return

    if args.notify_digest:
        notifier.send_daily_digest()
        return

    if args.notify_quota:
        notifier.send_quota_report()
        return

    if args.notify_keywords:
        notifier.send_keyword_report()
        return

    if args.health:
        import system_diagnostics
        print(system_diagnostics.format_daily_diagnostic_cli())
        return

    if args.weekly_status:
        import system_diagnostics
        print(system_diagnostics.format_weekly_status_cli())
        return

    if args.notify_health:
        notifier.send_bottleneck_report()
        return

    if args.notify_weekly:
        notifier.send_weekly_status_report()
        return

    config = email_agent.load_config()

    if args.offscreen:
        os.environ["JOB_AUTO_OFFSCREEN"] = "1"
    if args.quiet:
        os.environ["JOB_AUTO_QUIET"] = "1"

    # Headed flag resolution: --headed forces headed, --headless forces headless, else use config default (headless=True)
    force_headed = False
    if args.headed:
        force_headed = True
    elif args.headless:
        force_headed = False

    with MacKeepAwake(keep_screen_on=not args.no_screen):
        if args.login_naukri or args.login_all:
            print("[Unified Launcher] Starting Naukri login session...")
            naukri_agent.run_naukri_pipeline(mode="login", force_headed=True)

        if args.login_instahyre or args.login_all:
            print("[Unified Launcher] Starting Instahyre login session...")
            instahyre_agent.run_instahyre_pipeline(mode="login", force_headed=True)

        if args.login_linkedin or args.login_all:
            print("[Unified Launcher] Starting LinkedIn login session...")
            linkedin_agent.run_linkedin_pipeline(mode="login", force_headed=True)

        if args.login_indeed or args.login_all:
            print("[Unified Launcher] Starting Indeed India login session...")
            indeed_agent.run_indeed_pipeline(mode="login", force_headed=True)

        if args.login_shine or args.login_all:
            print("[Unified Launcher] Starting Shine.com login session...")
            shine_agent.run_shine_pipeline(mode="login", force_headed=True)

        if args.login_wellfound or args.login_all:
            print("[Unified Launcher] Starting Wellfound login session...")
            wellfound_agent.run_wellfound_pipeline(mode="login", force_headed=True)

        if args.login_jobgether or args.login_all:
            print("[Unified Launcher] Starting Jobgether login session...")
            jobgether_agent.run_jobgether_pipeline(mode="login", force_headed=True)

        if args.login_foundit or args.login_all:
            print("[Unified Launcher] Starting Foundit (Monster India) login session...")
            foundit_agent.run_foundit_pipeline(mode="login", force_headed=True)

        if args.login_forms:
            print("[Unified Launcher] Starting Google / Microsoft Forms interactive login session...")
            forms_agent.interactive_login(config)

        if args.refresh_all:
            print("[Unified Launcher] Refreshing profiles across all portals...")
            naukri_agent.run_naukri_pipeline(mode="refresh", force_headed=force_headed)
            instahyre_agent.run_instahyre_pipeline(mode="refresh", force_headed=force_headed)
            shine_agent.run_shine_pipeline(mode="refresh", force_headed=force_headed)
            wellfound_agent.run_wellfound_pipeline(mode="refresh", force_headed=force_headed)
            jobgether_agent.run_jobgether_pipeline(mode="refresh", force_headed=force_headed)
            foundit_agent.run_foundit_pipeline(mode="refresh", force_headed=force_headed)

        if args.refresh_shine:
            print("[Unified Launcher] Refreshing Shine.com profile...")
            shine_agent.run_shine_pipeline(mode="refresh", force_headed=force_headed)

        if args.refresh_wellfound:
            print("[Unified Launcher] Refreshing Wellfound profile activity...")
            wellfound_agent.run_wellfound_pipeline(mode="refresh", force_headed=force_headed)

        if args.refresh_jobgether:
            print("[Unified Launcher] Refreshing Jobgether profile...")
            jobgether_agent.run_jobgether_pipeline(mode="refresh", force_headed=force_headed)

        if args.refresh_foundit:
            print("[Unified Launcher] Refreshing Foundit profile...")
            foundit_agent.run_foundit_pipeline(mode="refresh", force_headed=force_headed)

        if args.harvest_linkedin:
            print("[Unified Launcher] Mining LinkedIn recruiter posts in past 24h...")
            linkedin_agent.run_linkedin_pipeline(mode="harvest", force_headed=force_headed)

        if args.apply_naukri:
            print("[Unified Launcher] Running Naukri auto-applier...")
            naukri_agent.run_naukri_pipeline(mode="apply", force_headed=force_headed)

        if args.apply_instahyre:
            print("[Unified Launcher] Running Instahyre auto-applier...")
            instahyre_agent.run_instahyre_pipeline(mode="apply", force_headed=force_headed)

        if args.apply_linkedin:
            print("[Unified Launcher] Running LinkedIn Easy Apply bot...")
            linkedin_agent.run_linkedin_pipeline(mode="apply", force_headed=force_headed)

        if args.apply_indeed:
            print("[Unified Launcher] Running Indeed India auto-applier...")
            indeed_agent.run_indeed_pipeline(mode="apply", force_headed=force_headed)

        if args.apply_shine:
            print("[Unified Launcher] Running Shine.com auto-applier...")
            shine_agent.run_shine_pipeline(mode="apply", force_headed=force_headed)

        if args.apply_wellfound:
            print("[Unified Launcher] Running Wellfound auto-applier...")
            wellfound_agent.run_wellfound_pipeline(mode="apply", force_headed=force_headed)

        if args.apply_jobgether:
            print("[Unified Launcher] Running Jobgether auto-applier...")
            jobgether_agent.run_jobgether_pipeline(mode="apply", force_headed=force_headed)

        if args.apply_foundit:
            print("[Unified Launcher] Running Foundit auto-applier...")
            foundit_agent.run_foundit_pipeline(mode="apply", force_headed=force_headed)

        if args.apply_greenhouse:
            print("[Unified Launcher] Running Greenhouse ATS auto-applier & board sweep...")
            greenhouse_agent.run_greenhouse_daily_sweep(config, dry_run=args.dry_run, force_headed=force_headed, review_mode=args.review)

        if args.greenhouse_url:
            print(f"[Unified Launcher] Applying directly to Greenhouse URL: {args.greenhouse_url}...")
            greenhouse_agent.apply_single_url(args.greenhouse_url, config, dry_run=args.dry_run, force_headed=force_headed, review_mode=args.review)

        if args.apply_workday:
            print("[Unified Launcher] Running Workday ATS auto-applier & portal sweep...")
            workday_agent.run_workday_daily_sweep(config, dry_run=args.dry_run, force_headed=force_headed, review_mode=args.review)

        if args.workday_url:
            print(f"[Unified Launcher] Applying directly to Workday URL: {args.workday_url}...")
            workday_agent.apply_single_url(args.workday_url, config, dry_run=args.dry_run, force_headed=force_headed, review_mode=args.review)

        if args.apply_forms:
            print("[Unified Launcher] Processing Google & Microsoft Forms dropzone tickets...")
            forms_agent.process_forms_dropzone(config, dry_run=args.dry_run, force_headed=force_headed, review_mode=args.review)

        if args.form_url:
            print(f"[Unified Launcher] Applying directly to Form: {args.form_url}...")
            forms_agent.apply_single_url(args.form_url, config, dry_run=args.dry_run, force_headed=force_headed, review_mode=args.review)

        if args.connect_linkedin:
            print("[Unified Launcher] Running LinkedIn connection outreach (Hiring Managers & Agency Recruiters)...")
            linkedin_agent.run_linkedin_pipeline(mode="connect", force_headed=force_headed)

        if args.optimize:
            print("[Unified Launcher] Running autonomous self-learning optimization cycle...")
            optimizer.run_optimization_cycle()

        if args.emails:
            print("[Unified Launcher] Processing email dropzone...")
            email_agent.process_dropzone()

        if args.cold_email:
            print("[Unified Launcher] Executing Direct Cold Outreach Pipeline...")
            if args.cold_email != "DISPATCH":
                added, skipped = cold_email_agent.ingest_source(args.cold_email)
                print(f"[Unified Launcher] Enqueued {added} lead(s) ({skipped} skipped/duplicates).")
            if args.dry_run:
                cold_email_agent.dispatch_cold_email_queue(dry_run=True, force=True)
            else:
                cold_email_agent.dispatch_cold_email_queue(force=True)

        if args.apply_all or args.all:
            quotas = quota_manager.get_all_quotas()
            wave_info = quotas["wave_info"]
            wave_label = f"Wave {wave_info['current_wave_num']}/{wave_info['total_waves']}" if wave_info['current_wave_num'] > 0 else "Full Sweep"

            print(f"\n[Unified Launcher] 🎯 Initiating Quota-Aware Automation ({wave_label})...")
            print(f"[Unified Launcher] Progress So Far Today: {quotas['total_applied']} / {quotas['total_limit']} ({quotas['overall_percent']}%) | Remaining: {quotas['total_remaining']}")

            if quotas["all_completed"]:
                print(f"\n[Unified Launcher] 🎉 All daily application goals ({quotas['total_limit']}/{quotas['total_limit']}) are 100% completed today!")
                print("[Unified Launcher] Standing by until tomorrow's first wave.\n")
                tg = notifier.TelegramNotifier()
                if tg.is_configured():
                    tg.send_message(
                        f"🎉 *DAILY APPLICATION GOAL FULLY MET!* (100%)\n"
                        f"All `{quotas['total_limit']}` applications and outreach targets have been satisfied today. System standing by."
                    )
            else:
                if args.all:
                    try:
                        print("\n[Unified Launcher] Phase 0: Executing Self-Learning & Intelligence Optimization...")
                        optimizer.run_optimization_cycle()
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 0 error (non-fatal): {e}")

                # Phase 1: Naukri
                nk_rem = quota_manager.get_remaining("naukri")
                if nk_rem <= 0:
                    print(f"[Unified Launcher] [Quota-Aware] ⏭️ Skipping Phase 1 (Naukri): Daily quota reached ({quotas['portals']['naukri']['applied']}/{quotas['portals']['naukri']['limit']}).")
                else:
                    try:
                        slice_target = quota_manager.get_wave_slice("naukri")
                        mode_str = "all" if args.all else "apply"
                        print(f"[Unified Launcher] Phase 1: Running Naukri ({mode_str.upper()}) (Target: {slice_target}, Remaining Today: {nk_rem})...")
                        naukri_agent.run_naukri_pipeline(mode=mode_str, max_applies=slice_target, force_headed=force_headed)
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 1 (Naukri) error (non-fatal): {e}")

                # Phase 2: Instahyre
                ih_rem = quota_manager.get_remaining("instahyre")
                if ih_rem <= 0:
                    print(f"[Unified Launcher] [Quota-Aware] ⏭️ Skipping Phase 2 (Instahyre): Daily quota reached ({quotas['portals']['instahyre']['applied']}/{quotas['portals']['instahyre']['limit']}).")
                else:
                    try:
                        slice_target = quota_manager.get_wave_slice("instahyre")
                        mode_str = "all" if args.all else "apply"
                        print(f"[Unified Launcher] Phase 2: Running Instahyre ({mode_str.upper()}) (Target: {slice_target}, Remaining Today: {ih_rem})...")
                        instahyre_agent.run_instahyre_pipeline(mode=mode_str, max_applies=slice_target, force_headed=force_headed)
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 2 (Instahyre) error (non-fatal): {e}")

                # Phase 3: LinkedIn
                li_rem = quota_manager.get_remaining("linkedin")
                li_c_rem = quota_manager.get_remaining("linkedin_connect")
                if li_rem <= 0 and (not args.all or li_c_rem <= 0):
                    print(f"[Unified Launcher] [Quota-Aware] ⏭️ Skipping Phase 3 (LinkedIn): Daily easy apply & outreach quotas reached.")
                else:
                    try:
                        li_slice = quota_manager.get_wave_slice("linkedin") if li_rem > 0 else 0
                        li_c_slice = quota_manager.get_wave_slice("linkedin_connect") if (args.all and li_c_rem > 0) else 0
                        mode_str = "all" if args.all else "apply"
                        print(f"[Unified Launcher] Phase 3: Running LinkedIn ({mode_str.upper()}) (Easy Apply Target: {li_slice}, Connect Target: {li_c_slice})...")
                        linkedin_agent.run_linkedin_pipeline(mode=mode_str, max_applies=li_slice, max_connect=li_c_slice, force_headed=force_headed)
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 3 (LinkedIn) error (non-fatal): {e}")

                # Phase 4: Indeed
                ind_rem = quota_manager.get_remaining("indeed")
                if ind_rem <= 0:
                    print(f"[Unified Launcher] [Quota-Aware] ⏭️ Skipping Phase 4 (Indeed): Daily quota reached ({quotas['portals']['indeed']['applied']}/{quotas['portals']['indeed']['limit']}).")
                else:
                    try:
                        slice_target = quota_manager.get_wave_slice("indeed")
                        print(f"[Unified Launcher] Phase 4: Running Indeed India Auto-Applier (Target: {slice_target}, Remaining Today: {ind_rem})...")
                        indeed_agent.run_indeed_pipeline(mode="apply", max_applies=slice_target, force_headed=force_headed)
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 4 (Indeed) error (non-fatal): {e}")

                # Phase 5: Shine
                sh_rem = quota_manager.get_remaining("shine")
                if sh_rem <= 0:
                    print(f"[Unified Launcher] [Quota-Aware] ⏭️ Skipping Phase 5 (Shine): Daily quota reached ({quotas['portals']['shine']['applied']}/{quotas['portals']['shine']['limit']}).")
                else:
                    try:
                        slice_target = quota_manager.get_wave_slice("shine")
                        mode_str = "all" if args.all else "apply"
                        print(f"[Unified Launcher] Phase 5: Running Shine.com ({mode_str.upper()}) (Target: {slice_target}, Remaining Today: {sh_rem})...")
                        shine_agent.run_shine_pipeline(mode=mode_str, max_applies=slice_target, force_headed=force_headed)
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 5 (Shine) error (non-fatal): {e}")

                # Phase 6: Wellfound
                wf_rem = quota_manager.get_remaining("wellfound")
                if wf_rem <= 0:
                    print(f"[Unified Launcher] [Quota-Aware] ⏭️ Skipping Phase 6 (Wellfound): Daily quota reached ({quotas['portals']['wellfound']['applied']}/{quotas['portals']['wellfound']['limit']}).")
                else:
                    try:
                        slice_target = quota_manager.get_wave_slice("wellfound")
                        mode_str = "all" if args.all else "apply"
                        print(f"[Unified Launcher] Phase 6: Running Wellfound ({mode_str.upper()}) (Target: {slice_target}, Remaining Today: {wf_rem})...")
                        wellfound_agent.run_wellfound_pipeline(mode=mode_str, max_applies=slice_target, force_headed=force_headed)
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 6 (Wellfound) error (non-fatal): {e}")

                # Phase 7: Jobgether
                jg_rem = quota_manager.get_remaining("jobgether")
                if jg_rem <= 0:
                    print(f"[Unified Launcher] [Quota-Aware] ⏭️ Skipping Phase 7 (Jobgether): Daily quota reached ({quotas['portals']['jobgether']['applied']}/{quotas['portals']['jobgether']['limit']}).")
                else:
                    try:
                        slice_target = quota_manager.get_wave_slice("jobgether")
                        mode_str = "all" if args.all else "apply"
                        print(f"[Unified Launcher] Phase 7: Running Jobgether Remote ({mode_str.upper()}) (Target: {slice_target}, Remaining Today: {jg_rem})...")
                        jobgether_agent.run_jobgether_pipeline(mode=mode_str, max_applies=slice_target, force_headed=force_headed)
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 7 (Jobgether) error (non-fatal): {e}")

                # Phase 8: Foundit
                fnd_rem = quota_manager.get_remaining("foundit")
                if fnd_rem <= 0:
                    print(f"[Unified Launcher] [Quota-Aware] ⏭️ Skipping Phase 8 (Foundit): Daily quota reached ({quotas['portals']['foundit']['applied']}/{quotas['portals']['foundit']['limit']}).")
                else:
                    try:
                        slice_target = quota_manager.get_wave_slice("foundit")
                        mode_str = "all" if args.all else "apply"
                        print(f"[Unified Launcher] Phase 8: Running Foundit ({mode_str.upper()}) (Target: {slice_target}, Remaining Today: {fnd_rem})...")
                        foundit_agent.run_foundit_pipeline(mode=mode_str, max_applies=slice_target, force_headed=force_headed)
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 8 (Foundit) error (non-fatal): {e}")

                # Phase 9: Greenhouse ATS
                gh_rem = quota_manager.get_remaining("greenhouse")
                if gh_rem <= 0:
                    print(f"[Unified Launcher] [Quota-Aware] ⏭️ Skipping Phase 9 (Greenhouse): Daily quota reached ({quotas['portals']['greenhouse']['applied']}/{quotas['portals']['greenhouse']['limit']}).")
                else:
                    try:
                        slice_target = quota_manager.get_wave_slice("greenhouse")
                        print(f"[Unified Launcher] Phase 9: Running Greenhouse ATS Board Sweep (Target: {slice_target}, Remaining Today: {gh_rem})...")
                        greenhouse_agent.run_greenhouse_daily_sweep(config, max_applies=slice_target, dry_run=args.dry_run, force_headed=force_headed, review_mode=args.review)
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 9 (Greenhouse) error (non-fatal): {e}")

                # Phase 10: Workday ATS
                wd_rem = quota_manager.get_remaining("workday")
                if wd_rem <= 0:
                    print(f"[Unified Launcher] [Quota-Aware] ⏭️ Skipping Phase 10 (Workday): Daily quota reached ({quotas['portals']['workday']['applied']}/{quotas['portals']['workday']['limit']}).")
                else:
                    try:
                        slice_target = quota_manager.get_wave_slice("workday")
                        print(f"[Unified Launcher] Phase 10: Running Workday ATS Portal Sweep (Target: {slice_target}, Remaining Today: {wd_rem})...")
                        workday_agent.run_workday_daily_sweep(config, max_applies=slice_target, dry_run=args.dry_run, force_headed=force_headed, review_mode=args.review)
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 10 (Workday) error (non-fatal): {e}")

                # Phase 10b: Forms
                forms_rem = quota_manager.get_remaining("forms")
                if forms_rem <= 0:
                    print(f"[Unified Launcher] [Quota-Aware] ⏭️ Skipping Phase 10b (Forms): Daily quota reached ({quotas['portals']['forms']['applied']}/{quotas['portals']['forms']['limit']}).")
                else:
                    try:
                        slice_target = quota_manager.get_wave_slice("forms")
                        print(f"[Unified Launcher] Phase 10b: Processing Google & Microsoft Forms Dropzone Tickets (Target: {slice_target}, Remaining Today: {forms_rem})...")
                        forms_agent.process_forms_dropzone(config, max_applies=slice_target, dry_run=args.dry_run, force_headed=force_headed, review_mode=args.review)
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 10b (Forms) error (non-fatal): {e}")

                if args.all:
                    # Phase 11: Emails
                    try:
                        print("\n[Unified Launcher] Phase 11: Processing Cold Email Dropzone...")
                        email_agent.process_dropzone()
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 11 (Emails) error (non-fatal): {e}")

                    # Phase 12: Direct Cold Outreach Leads
                    ce_rem = quota_manager.get_remaining("cold_email")
                    if ce_rem <= 0:
                        print(f"[Unified Launcher] [Quota-Aware] ⏭️ Skipping Phase 12 (Cold Outreach): Daily limit of 25 already reached ({quotas['portals']['cold_email']['applied']}/25).")
                    else:
                        try:
                            slice_target = quota_manager.get_wave_slice("cold_email")
                            print(f"[Unified Launcher] Phase 12: Dispatching Direct Cold Outreach Leads (Target: {slice_target}, Remaining Today: {ce_rem})...")
                            cold_email_agent.dispatch_cold_email_queue(limit=slice_target)
                        except Exception as e:
                            print(f"[Unified Launcher] Phase 12 (Cold Emails) error (non-fatal): {e}")

                    # Phase 13: Inbound Recruiter & Application Link Sweep
                    try:
                        print("\n[Unified Launcher] Phase 13: Inbound Recruiter Copilot Sweep...")
                        recruiter_copilot.run_recruiter_copilot_cycle(dry_run=args.dry_run, force_headed=force_headed)
                    except Exception as e:
                        print(f"[Unified Launcher] Phase 13 (Recruiter Copilot) error (non-fatal): {e}")

                # Wave Completion Assessment
                updated_quotas = quota_manager.get_all_quotas()
                up_wave = updated_quotas["wave_info"]
                pct = updated_quotas["overall_percent"]
                bar = quota_manager.format_progress_bar(pct, 10)

                if updated_quotas["all_completed"]:
                    naukri_agent.notify_macos(
                        "All Daily Quotas Completed!",
                        f"100% of daily targets reached ({updated_quotas['total_limit']}/{updated_quotas['total_limit']}).",
                        is_urgent=True
                    )
                    tg = notifier.TelegramNotifier()
                    if tg.is_configured():
                        tg.send_message(
                            f"🎉 *CONGRATULATIONS! ALL DAILY QUOTAS COMPLETED!* (100%)\n"
                            f"All `{updated_quotas['total_limit']}` application and outreach goals have been achieved today across all 12 pipelines."
                        )
                else:
                    naukri_agent.notify_macos(
                        f"Wave {up_wave['current_wave_num']} Complete",
                        f"{updated_quotas['total_applied']}/{updated_quotas['total_limit']} applied today ({pct}%). Next wave: {up_wave['next_wave_str']}.",
                        is_urgent=True
                    )
                    tg = notifier.TelegramNotifier()
                    if tg.is_configured():
                        tg.send_message(
                            f"🌊 *Wave {up_wave['current_wave_num']} of {up_wave['total_waves']} Sweep Finished!*\n"
                            f"📊 *Today's Progress:* `{bar} {pct}%` (`{updated_quotas['total_applied']} / {updated_quotas['total_limit']}`)\n"
                            f"⏳ *Balance Remaining:* `{updated_quotas['total_remaining']}` applications\n"
                            f"⏰ *Next Sweep:* `{up_wave['next_wave_str']}`\n\n"
                            f"💡 _Send `/quota` to see live visual breakdown._"
                        )

                # Broadcast comprehensive daily status briefing
                try:
                    print("\n[Unified Launcher] Dispatching Consolidated Daily Status Digest...")
                    notifier.send_daily_digest()
                except Exception as e:
                    print(f"[Notifier] Notice: Could not dispatch daily digest: {e}")

        show_dashboard_status()


if __name__ == "__main__":
    main()
