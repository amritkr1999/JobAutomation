#!/usr/bin/env python3
"""
Unified Notification & Human-in-the-Loop Intervention Engine
Supports:
  1. Telegram Bot (Interactive alerts, screenshots, inline buttons, bidirectional unblocking, digests)
  2. WhatsApp Alerts (CallMeBot / Twilio webhook fallback)
  3. macOS Notification Banners (power_utils fallback)
"""

import os
import sys
import json
import time
import urllib.parse
import urllib.request
import logging
from datetime import datetime

import requests
import threading
import subprocess

import power_utils
import keyword_tracker
import quota_manager
import qa_memory
import ai_question_solver
import pending_qa

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')


def load_config():
    """Load system configuration safely."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"[Notifier] Error reading config: {e}")
    return {}


class TelegramNotifier:
    """
    Direct Telegram Bot API Integration (zero external telegram library dependency).
    Supports markdown, full-resolution screenshots, inline keyboards, and polling response waits.
    """
    def __init__(self, bot_token=None, chat_id=None):
        self.config = load_config()
        tg_cfg = self.config.get("notifications", {}).get("telegram", {})
        self.bot_token = bot_token or tg_cfg.get("bot_token", "").strip()
        self.chat_id = chat_id or str(tg_cfg.get("chat_id", "")).strip()
        self.enabled = bool(tg_cfg.get("enabled", False) and self.bot_token and self.chat_id)
        self.send_screenshots = tg_cfg.get("send_screenshots", True)
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"

    def is_configured(self):
        return bool(self.bot_token and self.chat_id and self.bot_token != "YOUR_TELEGRAM_BOT_TOKEN" and self.chat_id != "YOUR_TELEGRAM_CHAT_ID")

    def _api_call(self, endpoint, method="POST", json_data=None, data=None, files=None, timeout=12):
        """Execute resilient API call with automatic SSL verification fallback."""
        url = f"{self.api_base}/{endpoint}"
        try:
            return requests.request(method, url, json=json_data, data=data, files=files, timeout=timeout)
        except requests.exceptions.Timeout:
            # Polling timeouts are expected in long-polling
            return None
        except requests.exceptions.SSLError:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            try:
                return requests.request(method, url, json=json_data, data=data, files=files, timeout=timeout, verify=False)
            except Exception:
                return None
        except Exception as e:
            logging.debug(f"[Telegram API] Request error ({endpoint}): {e}")
            return None

    def auto_link_chat(self):
        """
        Polls getUpdates to detect the chat_id from anyone who messaged the bot,
        and automatically saves it into config.json.
        """
        print(f"[Telegram Setup] Checking for messages sent to @mac_job_bot...")
        try:
            resp = self._api_call("getUpdates", method="GET", timeout=10)
            data = resp.json() if resp else {}
            if not data.get("ok"):
                print(f"[Telegram Setup] API Error: {data.get('description')}")
                return False

            updates = data.get("result", [])
            if not updates:
                print("\n⚠️ No messages detected yet.")
                print("👉 Step 1: Open Telegram on your phone or desktop.")
                print("👉 Step 2: Open your bot: https://t.me/mac_job_bot")
                print("👉 Step 3: Tap 'START' (or send /start or 'hello').")
                print("👉 Step 4: Run: python3 notifier.py --link\n")
                return False

            latest_update = updates[-1]
            chat_obj = latest_update.get("message", {}).get("chat", {}) or latest_update.get("callback_query", {}).get("message", {}).get("chat", {})
            user_obj = latest_update.get("message", {}).get("from", {}) or latest_update.get("callback_query", {}).get("from", {})

            chat_id = str(chat_obj.get("id", ""))
            first_name = user_obj.get("first_name", "User")
            username = user_obj.get("username", "")

            if not chat_id:
                print("Could not parse chat_id from recent updates.")
                return False

            print(f"\n🎉 Found Telegram user: {first_name} (@{username}) with Chat ID: {chat_id}")

            # Save to config.json
            cfg = load_config()
            cfg.setdefault("notifications", {}).setdefault("telegram", {})
            cfg["notifications"]["telegram"]["enabled"] = True
            cfg["notifications"]["telegram"]["bot_token"] = self.bot_token
            cfg["notifications"]["telegram"]["chat_id"] = chat_id

            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)

            self.chat_id = chat_id
            self.enabled = True
            print(f"✅ Successfully saved Chat ID {chat_id} to config.json!\n")

            # Send welcome message
            welcome_msg = (
                f"🤖 *Job Automation Bot Connected!*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Welcome, *{first_name}*! Your Telegram notification pipeline is now 100% active.\n\n"
                f"You will automatically receive:\n"
                f"• 🚨 *Human Intervention Alerts*: Live screenshots & inline buttons when Workday or Greenhouse needs an answer\n"
                f"• 📊 *Daily Status Summaries*: Automated digest after every job search & apply run\n"
                f"• 🎯 *Keyword Analytics*: Yield & application conversion stats\n\n"
                f"💬 Send `/status` or `/keywords` anytime to query your bot!"
            )
            self.send_message(welcome_msg)
            print("🚀 Welcome confirmation message sent to your Telegram chat!")
            return True
        except Exception as e:
            print(f"[Telegram Setup] Error during auto-link: {e}")
            return False

    def send_message(self, text, parse_mode="Markdown", reply_markup=None):
        """Send a formatted text message to Telegram chat."""
        if not self.is_configured():
            logging.debug("[Telegram] Bot not configured or disabled.")
            return None

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = self._api_call("sendMessage", json_data=payload, timeout=10)
            if not resp:
                return None
            data = resp.json()
            if not data.get("ok"):
                logging.warning(f"[Telegram] Failed to send message: {data.get('description')}")
                if "can't parse entities" in data.get("description", "").lower():
                    payload.pop("parse_mode", None)
                    resp2 = self._api_call("sendMessage", json_data=payload, timeout=10)
                    return resp2.json().get("result") if resp2 else None
            return data.get("result")
        except Exception as e:
            logging.error(f"[Telegram] Exception sending message: {e}")
            return None

    def send_photo(self, photo_path, caption=None, parse_mode="Markdown", reply_markup=None):
        """Send a screenshot image to Telegram chat."""
        if not self.is_configured():
            return None
        if not photo_path or not os.path.exists(photo_path):
            logging.warning(f"[Telegram] Photo path not found: {photo_path}")
            if caption:
                return self.send_message(caption, parse_mode=parse_mode, reply_markup=reply_markup)
            return None

        url = f"{self.api_base}/sendPhoto"
        data = {
            "chat_id": self.chat_id,
            "caption": caption[:1024] if caption else "",
            "parse_mode": parse_mode
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)

        try:
            with open(photo_path, "rb") as f:
                files = {"photo": f}
                resp = self._api_call("sendPhoto", data=data, files=files, timeout=20)
            if not resp:
                if caption:
                    return self.send_message(caption, parse_mode=parse_mode, reply_markup=reply_markup)
                return None
            res_data = resp.json()
            if not res_data.get("ok"):
                logging.warning(f"[Telegram] Failed to send photo: {res_data.get('description')}")
                if caption:
                    return self.send_message(caption, parse_mode=parse_mode, reply_markup=reply_markup)
            return res_data.get("result")
        except Exception as e:
            logging.error(f"[Telegram] Exception sending photo: {e}")
            if caption:
                return self.send_message(caption, parse_mode=parse_mode, reply_markup=reply_markup)
            return None

    def request_human_input(self, question, options=None, screenshot_path=None, timeout_seconds=180, default_answer=None):
        """
        Send a stuck/intervention alert with inline keyboard buttons and wait for user reply.
        Returns user-selected option or typed text, or default_answer on timeout.
        """
        if not self.is_configured():
            logging.info(f"[Telegram] Interactive intervention unavailable (Telegram not configured). Using default answer: {default_answer}")
            return default_answer

        # Prepare inline keyboard if options provided
        reply_markup = None
        if options and isinstance(options, list):
            keyboard_rows = []
            row = []
            for i, opt in enumerate(options[:6]):
                # Telegram callback data max 64 bytes
                callback_tag = f"ans_{i}"
                row.append({"text": str(opt)[:30], "callback_data": callback_tag})
                if len(row) == 2:
                    keyboard_rows.append(row)
                    row = []
            if row:
                keyboard_rows.append(row)
            keyboard_rows.append([{"text": "⏩ Skip This Job", "callback_data": "skip_job"}])
            reply_markup = {"inline_keyboard": keyboard_rows}

        prompt_text = (
            f"🚨 *HUMAN INTERVENTION REQUIRED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"❓ *Question / Problem:*\n`{question}`\n\n"
            f"⏳ _Waiting up to {timeout_seconds}s for your tap or text reply below..._"
        )

        sent_msg = None
        if screenshot_path and os.path.exists(screenshot_path) and self.send_screenshots:
            sent_msg = self.send_photo(screenshot_path, caption=prompt_text, reply_markup=reply_markup)
        else:
            sent_msg = self.send_message(prompt_text, reply_markup=reply_markup)

        if not sent_msg:
            return default_answer

        # Get current latest update ID to ignore old messages
        last_update_id = 0
        try:
            updates_init = requests.get(f"{self.api_base}/getUpdates?offset=-1", timeout=5).json()
            if updates_init.get("ok") and updates_init.get("result"):
                last_update_id = updates_init["result"][-1]["update_id"]
        except Exception:
            pass

        start_time = time.time()
        logging.info(f"[Telegram Intervention] Waiting {timeout_seconds}s for user input...")

        while (time.time() - start_time) < timeout_seconds:
            time.sleep(3)
            try:
                up_resp = self._api_call(f"getUpdates?offset={last_update_id + 1}&timeout=4", method="GET", timeout=8)
                if not up_resp:
                    continue
                data = up_resp.json()
                if not data.get("ok"):
                    continue

                for update in data.get("result", []):
                    last_update_id = update["update_id"]

                    # 1. Check for Callback Query (Inline Keyboard Button Click)
                    if "callback_query" in update:
                        cb = update["callback_query"]
                        cb_id = cb.get("id")
                        cb_data = cb.get("data", "")
                        from_user = cb.get("from", {}).get("first_name", "User")

                        # Acknowledge callback to stop button spinner
                        try:
                            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id}, timeout=3)
                        except Exception:
                            pass

                        chosen_val = None
                        if cb_data == "skip_job":
                            chosen_val = "SKIP"
                        elif cb_data.startswith("ans_") and options:
                            try:
                                idx = int(cb_data.split("_")[1])
                                chosen_val = options[idx]
                            except Exception:
                                pass

                        if chosen_val:
                            self.send_message(f"✅ *Received Choice:* `{chosen_val}`\n_Resuming application..._")
                            return chosen_val

                    # 2. Check for Typed Text Message
                    if "message" in update and "text" in update["message"]:
                        msg = update["message"]
                        sender_id = str(msg.get("chat", {}).get("id", ""))
                        if sender_id == self.chat_id:
                            typed_text = msg["text"].strip()
                            # Ignore slash commands during active question
                            if not typed_text.startswith("/"):
                                self.send_message(f"✅ *Answer Captured:* `{typed_text}`\n_Resuming application..._")
                                return typed_text

            except Exception as e:
                logging.debug(f"[Telegram Polling] Exception: {e}")

        # Timeout reached
        self.send_message(
            f"⏳ *Intervention Timed Out ({timeout_seconds}s).*\n"
            f"Proceeding with default: `{default_answer or 'Skip / Heuristic'}`"
        )
        return default_answer


class WhatsAppNotifier:
    """
    Lightweight WhatsApp Notification Gateway (CallMeBot / Webhook).
    """
    def __init__(self):
        self.config = load_config()
        self.wa_cfg = self.config.get("notifications", {}).get("whatsapp", {})
        self.enabled = bool(self.wa_cfg.get("enabled", False))
        self.phone = str(self.wa_cfg.get("phone_number", "")).strip()
        self.api_key = str(self.wa_cfg.get("api_key", "")).strip()

    def is_configured(self):
        return bool(self.enabled and self.phone and self.api_key)

    def send_message(self, text):
        """Send a WhatsApp notification via CallMeBot gateway."""
        if not self.is_configured():
            return False
        try:
            clean_text = urllib.parse.quote_plus(text[:1500])
            url = f"https://api.callmebot.com/whatsapp.php?phone={self.phone}&text={clean_text}&apikey={self.api_key}"
            resp = requests.get(url, timeout=12)
            return resp.status_code == 200
        except Exception as e:
            logging.error(f"[WhatsApp] Failed to send message: {e}")
            return False


class UnifiedNotifier:
    """
    Master Dispatcher for Alerts, Daily Summaries, Keyword Analytics & Human Intervention.
    """
    def __init__(self):
        self.tg = TelegramNotifier()
        self.wa = WhatsAppNotifier()

    def notify_stuck(self, platform, company, role, reason, question=None, url=None, screenshot_path=None, options=None, timeout_seconds=180, default_answer=None):
        """
        High-level hook called by agents when stuck at any step or question.
        Returns the human-resolved answer if provided, or default_answer.
        """
        title = f"⚠️ [{platform}] System Paused: {company}"
        details = (
            f"🏢 *Company:* `{company}`\n"
            f"💼 *Role:* `{role}`\n"
            f"🌐 *Platform:* `{platform}`\n"
            f"🛑 *Reason:* {reason}\n"
        )
        if url:
            details += f"🔗 *Job URL:* [Open Link]({url})\n"
        if question:
            details += f"\n❓ *Question / Field:* `{question}`\n"

        # 0. Check universal persistent QA memory and AI deterministic solver first
        if question:
            saved_ans = qa_memory.recall_answer(question, available_options=options)
            if not saved_ans:
                saved_ans = ai_question_solver.classify_and_solve_deterministic(question, options=options)
                if saved_ans:
                    qa_memory.save_learned_answer(question, saved_ans, platform=platform)

            if saved_ans:
                logging.info(f"[QA Memory / AI Solver] Auto-resolved known question without prompting: '{question[:50]}...' -> '{saved_ans}'")
                return saved_ans

        # Sanitize / enhance default_answer using AI question solver
        if question and (not default_answer or "building scalable applications" in str(default_answer)):
            try:
                default_answer = ai_question_solver.solve_screening_question(question, options=options, platform=platform)
            except Exception as e:
                logging.debug(f"[Notifier] AI fallback computation error: {e}")

        # 1. Trigger local macOS notification banner immediately
        power_utils.notify_macos(
            f"Automation Paused: {platform} - {company}",
            f"{reason[:80]}: {question or 'Action required.'}",
            is_urgent=True
        )

        # 2. If Telegram is configured and an interactive question is raised, wait for human reply
        if self.tg.is_configured() and question:
            prompt_q = f"[{platform}] {company} ({role})\n{question}"
            user_response = self.tg.request_human_input(
                question=prompt_q,
                options=options,
                screenshot_path=screenshot_path,
                timeout_seconds=timeout_seconds,
                default_answer=default_answer
            )
            if user_response and user_response not in ("SKIP", default_answer):
                qa_memory.save_learned_answer(question, user_response, platform=platform)
            elif user_response == default_answer and question:
                # Timed out or used default: keep for later review so Amrit can customize anytime
                pending_entry = pending_qa.add_pending_question(
                    question=question,
                    options=options,
                    platform=platform,
                    company=company,
                    role=role,
                    default_answered=default_answer
                )
                qid = pending_entry.get("id")
                btn_markup = {
                    "inline_keyboard": [
                        [{"text": "✏️ Set My Preferred Answer", "callback_data": f"review_qa:{qid}"}],
                        [{"text": "📋 View Pending Questions (/review)", "callback_data": "list_pending_qa"}]
                    ]
                }
                self.tg.send_message(
                    f"📝 *Saved to Your Question Review Bank!*\n\n"
                    f"❓ *Question:* `{question}`\n"
                    f"🤖 *Auto-Answered with:* `{default_answer}`\n\n"
                    f"_You can set your preferred answer anytime whenever you get time—tap below, type `/review`, or edit in your Web Dashboard._",
                    reply_markup=btn_markup
                )
            return user_response

        # 3. Otherwise, send broadcast alert with screenshot
        if self.tg.is_configured():
            alert_text = f"🚨 *AUTOMATION ALERT: SYSTEM STUCK*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n{details}"
            if screenshot_path and os.path.exists(screenshot_path) and self.tg.send_screenshots:
                self.tg.send_photo(screenshot_path, caption=alert_text)
            else:
                self.tg.send_message(alert_text)

        # 4. Mirror to WhatsApp if enabled
        if self.wa.is_configured():
            wa_text = f"⚠️ JobAutomation Alert [{platform} - {company}]: {reason}. {question or ''}"
            self.wa.send_message(wa_text)

        return default_answer

    def send_daily_digest(self):
        """
        Generate and broadcast a rich daily executive summary across all 10 portals.
        """
        today_str = datetime.now().strftime("%Y-%m-%d")
        now_time = datetime.now().strftime("%I:%M %p")
        config = load_config()
        logs_dir = os.path.expanduser(config.get('paths', {}).get('logs', '~/Documents/JobAutomation/logs'))

        def _count_portal(filename, status_keys=("APPLIED", "SUBMITTED")):
            p = os.path.join(logs_dir, filename)
            if not os.path.exists(p):
                return 0, 0
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    jobs = data.get("applied_jobs", {})
                    today_cnt = sum(
                        1 for v in jobs.values()
                        if v.get("status") in status_keys and str(v.get("applied_at", "")).startswith(today_str)
                    )
                    return today_cnt, len(jobs)
            except Exception:
                return 0, 0

        # Gather metrics
        gh_today, gh_total = _count_portal("greenhouse_applied_jobs.json")
        wd_today, wd_total = _count_portal("workday_applied_jobs.json")
        nk_today, nk_total = _count_portal("naukri_applied_jobs.json")
        ih_today, ih_total = _count_portal("instahyre_applied_jobs.json")
        ind_today, ind_total = _count_portal("indeed_applied_jobs.json")
        sh_today, sh_total = _count_portal("shine_applied_jobs.json")
        wf_today, wf_total = _count_portal("wellfound_applied_jobs.json")
        jg_today, jg_total = _count_portal("jobgether_applied_jobs.json")
        fnd_today, fnd_total = _count_portal("foundit_applied_jobs.json")

        # LinkedIn
        li_path = os.path.join(logs_dir, "linkedin_applied_jobs.json")
        li_today, li_harvest = 0, 0
        if os.path.exists(li_path):
            try:
                with open(li_path, 'r', encoding='utf-8') as f:
                    ldata = json.load(f)
                    ljobs = ldata.get("applied_jobs", {})
                    li_today = sum(1 for v in ljobs.values() if v.get("status") == "APPLIED" and str(v.get("applied_at", "")).startswith(today_str))
                    lharv = ldata.get("harvested_posts", {})
                    li_harvest = sum(1 for v in lharv.values() if str(v.get("harvested_at", "")).startswith(today_str))
            except Exception:
                pass

        # Emails
        sent_path = os.path.join(logs_dir, "sent_emails.json")
        cold_sent_today = 0
        if os.path.exists(sent_path):
            try:
                with open(sent_path, 'r', encoding='utf-8') as f:
                    sdata = json.load(f)
                    cold_sent_today = sum(1 for v in sdata.values() if str(v.get("sent_at", "")).startswith(today_str) and v.get("application_type") == "COLD_OUTREACH")
            except Exception:
                pass

        total_applies_today = gh_today + wd_today + nk_today + ih_today + ind_today + sh_today + wf_today + jg_today + fnd_today + li_today

        # Keyword stats
        kw_summary = keyword_tracker.get_daily_keyword_summary(today_str)
        top_queries = sorted(kw_summary["by_query"].items(), key=lambda x: x[1]["applied"], reverse=True)[:3]
        if top_queries:
            top_queries_str = "\n".join([f"   • `{q}`: {data['applied']} applies ({data['scanned']} scanned)" for q, data in top_queries])
        else:
            top_queries_str = "   • _No keyword queries executed yet today._"

        # Quota and Wave intelligence
        quotas = quota_manager.get_all_quotas(today_str)
        wave_info = quotas["wave_info"]
        curr_wave = f"Wave {wave_info['current_wave_num']}/5" if wave_info['current_wave_num'] > 0 else "Pre-Wave 1"
        next_wave = wave_info["next_wave_str"]
        overall_bar = quota_manager.format_progress_bar(quotas["overall_percent"], 8)

        message = (
            f"📊 *JOB AUTOMATION STATUS — {datetime.now().strftime('%d %b %Y')}*\n"
            f"🕒 *Snapshot Checked:* `{now_time} IST`\n"
            f"🌊 *Paced Schedule:* `{curr_wave}` | *Next Wave:* `{next_wave}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 *OVERALL PROGRESS:* `{overall_bar} {quotas['overall_percent']}%`\n"
            f"   • *Submitted Today:* `{quotas['total_applied']} / {quotas['total_limit']}` Applications\n"
            f"   • *Remaining Balance:* `{quotas['total_remaining']}` Applications\n\n"
            f"🏢 *Enterprise & ATS Portals:*\n"
            f"   • 🏢 Greenhouse ATS: `{gh_today}` today (Stripe, Databricks, Cockroach)\n"
            f"   • 💼 Workday ATS:    `{wd_today}` today (Autodesk, Adobe, Nvidia)\n\n"
            f"🌐 *Job Boards & Portals:*\n"
            f"   • 💼 Naukri.com:    `{nk_today}` today\n"
            f"   • ⚡ Instahyre:     `{ih_today}` today\n"
            f"   • 🌐 LinkedIn:      `{li_today}` Easy Applies | `{li_harvest}` Posts Mined\n"
            f"   • 🔍 Indeed India:  `{ind_today}` today\n"
            f"   • ✨ Shine.com:     `{sh_today}` today\n"
            f"   • 🚀 Wellfound:     `{wf_today}` today\n"
            f"   • 🌍 Jobgether:     `{jg_today}` today\n"
            f"   • 🎯 Foundit:       `{fnd_today}` today\n\n"
            f"📧 *Direct Cold Email Outreach:*\n"
            f"   • Dispatched Today: `{cold_sent_today}` Personalized Emails\n\n"
            f"🔑 *Top Yield Keywords Today:*\n{top_queries_str}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 _Tip: Send `/quota` for live visual progress bars across all platforms!_\n"
            f"💡 _Tip: Send `/run all` or `/run [portal]` to start a sweep right now!_"
        )
        sent_tg = False
        if self.tg.is_configured():
            sent_tg = bool(self.tg.send_message(message))

        if self.wa.is_configured():
            short_wa = f"📊 Daily Job Briefing ({datetime.now().strftime('%d %b')}): {total_applies_today} applied across 10 platforms (GH: {gh_today}, WD: {wd_today}, Naukri: {nk_today}, LI: {li_today}). Cold Emails: {cold_sent_today}."
            self.wa.send_message(short_wa)

        print("\n" + "=" * 60)
        print(message)
        print("=" * 60 + "\n")

        power_utils.notify_macos(
            "Daily Job Automation Complete",
            f"{total_applies_today} applications submitted today across 10 platforms.",
            is_urgent=True
        )

        return sent_tg

    def send_quota_report(self):
        """Send the formatted daily quota progress report with progress bars."""
        report = quota_manager.format_quota_progress_markdown()
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60 + "\n")
        if self.tg.is_configured():
            return self.tg.send_message(report)
        return False

    def send_keyword_report(self):
        """Send the formatted keyword intelligence report."""
        report = keyword_tracker.format_keyword_report_markdown()
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60 + "\n")
        if self.tg.is_configured():
            return self.tg.send_message(report)
        return False

    def send_tasks_report(self):
        """Send pending candidate action items with inline buttons."""
        try:
            import task_manager
            text, reply_markup = task_manager.format_tasks_for_telegram()
            print("\n" + "=" * 60)
            print(text)
            print("=" * 60 + "\n")
            if self.tg.is_configured():
                return self.tg.send_message(text, reply_markup=reply_markup)
        except Exception as e:
            logging.error(f"[Notifier] Error sending tasks report: {e}")
        return False

    def send_crm_report(self):
        """Send recruiter relationship pipeline summary."""
        try:
            import crm_tracker
            summary = crm_tracker.get_summary()
            breakdown = "\n".join([f"• *{k.replace('_', ' ')}:* `{v}`" for k, v in summary['stage_breakdown'].items() if v > 0])
            report = (
                f"📊 *RECRUITER PIPELINE & CRM SNAPSHOT*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👥 *Total Tracked Contacts:* `{summary['total_contacts']}`\n"
                f"🎯 *Active Engagements:* `{summary['active_engagements']}`\n\n"
                f"*Pipeline Stages:*\n{breakdown}\n\n"
                f"💡 _Use CLI `python3 run_all.py --crm` to view complete recruiter contact details._"
            )
            print("\n" + "=" * 60)
            print(report)
            print("=" * 60 + "\n")
            if self.tg.is_configured():
                return self.tg.send_message(report)
        except Exception as e:
            logging.error(f"[Notifier] Error sending CRM report: {e}")
        return False


# Global singleton instance for easy import
_global_notifier = None

def get_notifier():
    global _global_notifier
    if _global_notifier is None:
        _global_notifier = UnifiedNotifier()
    return _global_notifier


def notify_stuck(platform, company, role, reason, question=None, url=None, screenshot_path=None, options=None, timeout_seconds=180, default_answer=None):
    """Convenience functional interface for agents."""
    return get_notifier().notify_stuck(
        platform=platform,
        company=company,
        role=role,
        reason=reason,
        question=question,
        url=url,
        screenshot_path=screenshot_path,
        options=options,
        timeout_seconds=timeout_seconds,
        default_answer=default_answer
    )


def send_daily_digest():
    """Convenience function to trigger daily digest."""
    return get_notifier().send_daily_digest()


def send_quota_report():
    """Convenience function to trigger quota progress report."""
    return get_notifier().send_quota_report()


def send_keyword_report():
    """Convenience function to trigger keyword report."""
    return get_notifier().send_keyword_report()


def send_tasks_report():
    """Convenience function to trigger tasks report."""
    return get_notifier().send_tasks_report()


def send_crm_report():
    """Convenience function to trigger recruiter CRM report."""
    return get_notifier().send_crm_report()


# ---------------------------------------------------------------------------
# Interactive Bot Command Listener Daemon
# ---------------------------------------------------------------------------
def run_telegram_bot_listener():
    """
    Long-polling daemon for Telegram slash commands:
      /quota        - Show live visual progress bars & remaining daily balance
      /status       - Show live snapshot of today's automation
      /keywords     - Show keyword yield and performance
      /review       - Review & set preferred answers for default-answered questions
      /tasks        - Show open candidate action items
      /crm          - Show active recruiter conversations
      /run [portal] - Trigger an on-demand application sweep
      /help         - Show available commands
    """
    tg = TelegramNotifier()
    if not tg.is_configured():
        print("[Telegram Bot] Bot Token or Chat ID not configured in config.json. Exiting listener.")
        return

    print(f"[Telegram Bot] 🤖 Bot listener started for Chat ID: {tg.chat_id}. Polling for commands...", flush=True)
    last_update_id = 0
    active_review_state = {"qid": None, "question": None, "default": None}

    while True:
        try:
            resp = tg._api_call(f"getUpdates?offset={last_update_id + 1}&timeout=20", method="GET", timeout=30)
            if not resp:
                time.sleep(3)
                continue
            data = resp.json()
            if not data.get("ok"):
                time.sleep(3)
                continue

            for update in data.get("result", []):
                last_update_id = update["update_id"]

                # 1. Handle Inline Keyboard Button Callbacks
                if "callback_query" in update:
                    cb = update["callback_query"]
                    cb_id = cb.get("id")
                    cb_data = cb.get("data", "")
                    cb_chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))

                    if cb_chat_id == tg.chat_id and cb_data:
                        try:
                            tg._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id}, timeout=3)
                        except Exception:
                            pass

                        # Review QA Callback Handlers
                        if cb_data.startswith("review_qa:"):
                            qid = cb_data.split("review_qa:", 1)[1]
                            store = pending_qa.load_pending_reviews()
                            q_data = store.get("questions", {}).get(qid)
                            if not q_data:
                                tg.send_message("⚠️ Question not found or already answered.")
                                continue

                            active_review_state["qid"] = qid
                            active_review_state["question"] = q_data.get("question", "")
                            active_review_state["default"] = q_data.get("default_answered", "")

                            rows = []
                            opts = q_data.get("options", [])
                            if opts:
                                r = []
                                for idx, opt in enumerate(opts[:6]):
                                    r.append({"text": str(opt)[:30], "callback_data": f"ans_qa:{qid}:{idx}"})
                                    if len(r) == 2:
                                        rows.append(r)
                                        r = []
                                if r:
                                    rows.append(r)

                            rows.append([{"text": f"✅ Keep Default ({q_data.get('default_answered', '')[:20]})", "callback_data": f"keep_default_qa:{qid}"}])
                            rows.append([{"text": "🗑️ Dismiss Without Saving", "callback_data": f"dismiss_qa:{qid}"}])

                            prompt_msg = (
                                f"❓ *Review Screening Question*\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"`{q_data.get('question')}`\n\n"
                                f"🏢 *Platform:* `{q_data.get('platform')}` - `{q_data.get('company')}`\n"
                                f"🤖 *Default Used:* `{q_data.get('default_answered')}`\n\n"
                                f"👇 *Tap an option button below, OR type your answer as a text message right now:*"
                            )
                            tg.send_message(prompt_msg, reply_markup={"inline_keyboard": rows})

                        elif cb_data.startswith("ans_qa:"):
                            parts = cb_data.split(":")
                            qid = parts[1]
                            idx = int(parts[2])
                            store = pending_qa.load_pending_reviews()
                            q_data = store.get("questions", {}).get(qid, {})
                            opts = q_data.get("options", [])
                            if 0 <= idx < len(opts):
                                choice = opts[idx]
                                pending_qa.resolve_pending_question(qid, choice)
                                active_review_state["qid"] = None
                                tg.send_message(
                                    f"✅ *Preferred Answer Saved!*\n\n"
                                    f"❓ *Question:* `{q_data.get('question')}`\n"
                                    f"💾 *Saved Answer:* `{choice}`\n\n"
                                    f"_Next time this question appears on any ATS, this answer will be used automatically._"
                                )

                        elif cb_data.startswith("keep_default_qa:"):
                            qid = cb_data.split("keep_default_qa:", 1)[1]
                            store = pending_qa.load_pending_reviews()
                            q_data = store.get("questions", {}).get(qid, {})
                            def_ans = q_data.get("default_answered", "Yes")
                            pending_qa.resolve_pending_question(qid, def_ans)
                            active_review_state["qid"] = None
                            tg.send_message(
                                f"✅ *Locked In!* Saved `{def_ans}` as your permanent preference for `{q_data.get('question')}`."
                            )

                        elif cb_data.startswith("dismiss_qa:"):
                            qid = cb_data.split("dismiss_qa:", 1)[1]
                            pending_qa.dismiss_pending_question(qid)
                            active_review_state["qid"] = None
                            tg.send_message("🗑️ Question dismissed from pending review.")

                        elif cb_data == "list_pending_qa":
                            pending_items = pending_qa.get_pending_questions(status="PENDING")
                            if not pending_items:
                                tg.send_message("🎉 *All Caught Up!* No questions are currently pending review.")
                            else:
                                msg = f"📋 *Pending Screening Questions ({len(pending_items)}):*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                for idx, p in enumerate(pending_items[:5], 1):
                                    msg += f"{idx}. *[{p['platform']}]* `{p['question'][:55]}`\n   _Default used: {p['default_answered']}_\n"
                                first_id = pending_items[0]["id"]
                                tg.send_message(
                                    msg,
                                    reply_markup={"inline_keyboard": [[{"text": "✏️ Answer First Question", "callback_data": f"review_qa:{first_id}"}]]}
                                )

                        elif cb_data.startswith("approve_reply:"):
                            draft_id = cb_data.split("approve_reply:", 1)[1]
                            import reply_agent
                            tg.send_message(f"⏳ _Sending email reply for draft `{draft_id}`..._")
                            success = reply_agent.approve_and_send_draft(draft_id)
                            if success:
                                tg.send_message(f"🚀 *Reply Sent!* Successfully dispatched drafted response via SMTP.")
                            else:
                                tg.send_message(f"⚠️ Failed to dispatch draft `{draft_id}`. Please check system logs.")

                        elif cb_data.startswith("dismiss_reply:"):
                            draft_id = cb_data.split("dismiss_reply:", 1)[1]
                            import reply_agent
                            drafts_data = reply_agent.load_drafts()
                            if draft_id in drafts_data.get("drafts", {}):
                                drafts_data["drafts"][draft_id]["status"] = "DISMISSED"
                                reply_agent.save_drafts(drafts_data)
                            tg.send_message(f"🗑️ Draft `{draft_id}` dismissed.")

                        elif cb_data.startswith("complete_task:"):
                            task_id = cb_data.split("complete_task:", 1)[1]
                            import task_manager
                            task_manager.complete_task(task_id, completion_note="Completed via Telegram inline button.")
                            tg.send_message(f"✅ *Task Completed!* Marked `{task_id}` as done.")
                    continue

                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip()

                if chat_id != tg.chat_id or not text:
                    continue

                print(f"[Telegram Bot] Received command: {text}", flush=True)

                # Check if user is responding to an active QA review prompt with custom text
                if active_review_state.get("qid") and not text.startswith("/"):
                    qid = active_review_state["qid"]
                    q_text = active_review_state.get("question", "")
                    pending_qa.resolve_pending_question(qid, text)
                    active_review_state["qid"] = None
                    tg.send_message(
                        f"✅ *Preferred Answer Saved!*\n\n"
                        f"❓ *Question:* `{q_text}`\n"
                        f"💾 *Your Answer:* `{text}`\n\n"
                        f"_From now on, all applications across all portals will use this answer automatically._"
                    )
                    rem = pending_qa.get_pending_questions(status="PENDING")
                    if rem:
                        tg.send_message(
                            f"📝 *You have {len(rem)} more pending question(s).* Type `/review` to answer the next one!",
                            reply_markup={"inline_keyboard": [[{"text": "✏️ Answer Next Question", "callback_data": f"review_qa:{rem[0]['id']}"}]]}
                        )
                    continue

                try:
                    if text.lower() in ("/quota", "/progress", "quota", "progress"):
                        tg.send_message("⏳ _Calculating live quota progress across 12 platforms..._")
                        get_notifier().send_quota_report()

                    elif text.lower() in ("/status", "/today", "status"):
                        tg.send_message("⏳ _Checking live system ledger..._")
                        get_notifier().send_daily_digest()

                    elif text.lower() in ("/review", "/pending", "/qa", "/questions", "review", "pending", "qa"):
                        pending_items = pending_qa.get_pending_questions(status="PENDING")
                        if not pending_items:
                            tg.send_message("🎉 *All Caught Up!* No screening questions are currently pending review.\n_All applications are using your learned preferences._")
                        else:
                            msg = f"📋 *Pending Screening Questions Review ({len(pending_items)})*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            for idx, p in enumerate(pending_items[:5], 1):
                                msg += f"{idx}. *[{p['platform']}]* `{p['question'][:55]}`\n   _Default used: {p['default_answered']}_\n"
                            first_id = pending_items[0]["id"]
                            tg.send_message(
                                msg,
                                reply_markup={"inline_keyboard": [
                                    [{"text": "✏️ Answer Question #1", "callback_data": f"review_qa:{first_id}"}],
                                    [{"text": "🌐 Open Web Dashboard", "url": "http://127.0.0.1:8765"}]
                                ]}
                            )

                    elif text.lower() in ("/keywords", "/keyword", "keywords"):
                        get_notifier().send_keyword_report()

                    elif text.lower() in ("/tasks", "/task", "tasks", "task"):
                        get_notifier().send_tasks_report()

                    elif text.lower() in ("/pipeline", "/crm", "/track", "pipeline", "crm", "track"):
                        get_notifier().send_crm_report()

                    elif text.lower() in ("/inbound", "/sweep", "inbound", "sweep"):
                        tg.send_message("📥 *Starting Inbound Recruiter Sweep...*\n_Checking Gmail INBOX, extracting recruiter links, and preparing context-aware reply drafts._")
                        def _run_inbound():
                            try:
                                import recruiter_copilot
                                res = recruiter_copilot.run_recruiter_copilot_cycle()
                                msg = (
                                    f"🎉 *Recruiter Inbound Sweep Finished!*\n"
                                    f"• New Messages: `{res.get('new_messages_ingested', 0)}`\n"
                                    f"• Links Auto-Applied: `{res.get('links_successful', 0)}`\n"
                                    f"• Draft Replies Ready: `{res.get('reply_drafts_prepared', 0)}`\n"
                                    f"• Pending Tasks: `{res.get('active_pending_tasks', 0)}`"
                                )
                                tg.send_message(msg)
                            except Exception as err:
                                tg.send_message(f"⚠️ Error running inbound recruiter sweep: {err}")
                        threading.Thread(target=_run_inbound, daemon=True).start()

                    elif text.lower().startswith(("/run", "/apply")):
                        parts = text.split()
                        target = parts[1].lower() if len(parts) > 1 else "all"

                        valid_targets = {
                            "all": ["--all"],
                            "inbound": ["--inbound"],
                            "greenhouse": ["--apply-greenhouse"],
                            "workday": ["--apply-workday"],
                            "naukri": ["--apply-naukri"],
                            "instahyre": ["--apply-instahyre"],
                            "linkedin": ["--apply-linkedin"],
                            "indeed": ["--apply-indeed"],
                            "shine": ["--apply-shine"],
                            "wellfound": ["--apply-wellfound"],
                            "foundit": ["--apply-foundit"],
                            "forms": ["--apply-forms"],
                            "cold": ["--cold-email"],
                        }

                        if target not in valid_targets:
                            avail = ", ".join([f"`/run {k}`" for k in valid_targets.keys()])
                            tg.send_message(f"❓ Unknown target `{target}`.\nAvailable options: {avail}")
                            continue

                        flags = valid_targets[target]
                        tg.send_message(f"🚀 *Triggered On-Demand Run:* `{target.upper()}`\n_Executing in background on your Mac. You will receive an updated status briefing when finished!_")

                        def _run_bg(target_name, cli_flags):
                            try:
                                run_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_all.py")
                                cmd = [sys.executable, run_script] + cli_flags
                                subprocess.run(cmd, capture_output=True, text=True, timeout=900)
                                tg.send_message(f"✅ *{target_name.upper()} Run Completed!*")
                                get_notifier().send_daily_digest()
                            except Exception as err:
                                tg.send_message(f"⚠️ Error executing {target_name}: {err}")

                        threading.Thread(target=_run_bg, args=(target, flags), daemon=True).start()

                    elif text.lower() in ("/help", "/start", "help", "hello", "hi"):
                        help_text = (
                            "🤖 *JOB AUTOMATION BOT COMMANDS*\n"
                            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            "• `/review` - Review & set preferred answers for default-answered questions\n"
                            "• `/tasks` - Pending recruiter tasks, interviews & action items\n"
                            "• `/pipeline` - Recruiter CRM relationship stages & responses\n"
                            "• `/inbound` - Trigger on-demand recruiter email sweep & link apply\n"
                            "• `/quota` - Live visual progress bars & remaining daily balance\n"
                            "• `/status` - Today's applications summary & keyword yields\n"
                            "• `/keywords` - Today's keyword performance & yield stats\n"
                            "• `/run all` - Trigger full paced application wave right now\n"
                            "• `/run [portal]` - Trigger specific portal (e.g. `/run greenhouse`, `/run workday`, `/run forms`, `/run inbound`)\n"
                            "• `/help` - Show this commands guide\n\n"
                            "💡 _When a recruiter sends an application link or reply draft, you will receive an interactive card here with one-click approval buttons._"
                        )
                        tg.send_message(help_text)
                except Exception as cmd_err:
                    logging.exception(f"[Telegram Bot] Error executing command '{text}': {cmd_err}")
                    tg.send_message(f"⚠️ Error executing `{text}`: {cmd_err}")

        except Exception as e:
            logging.debug(f"[Telegram Bot] Listener loop error: {e}")
            time.sleep(3)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--link":
        TelegramNotifier().auto_link_chat()
    elif len(sys.argv) > 1 and sys.argv[1] == "--bot":
        run_telegram_bot_listener()
    elif len(sys.argv) > 1 and sys.argv[1] == "--digest":
        send_daily_digest()
    elif len(sys.argv) > 1 and sys.argv[1] == "--quota":
        send_quota_report()
    elif len(sys.argv) > 1 and sys.argv[1] == "--keywords":
        send_keyword_report()
    elif len(sys.argv) > 1 and sys.argv[1] == "--test":
        notifier = get_notifier()
        print("Testing Telegram configuration...")
        if notifier.tg.is_configured():
            res = notifier.tg.send_message("🔔 *Job Automation Test:* Notification engine is successfully connected!")
            print(f"Telegram Test Result: {'SUCCESS' if res else 'FAILED'}")
        else:
            print("Telegram not fully linked yet (missing chat_id in config.json). Testing local macOS banner...")
            power_utils.notify_macos("Job Automation Test", "Notification engine test passed!", is_urgent=True)
            print("macOS banner triggered.")
    else:
        print("Usage: python3 notifier.py [--link | --test | --digest | --quota | --keywords | --bot]")
