#!/usr/bin/env python3
"""
Power & Display Management Utilities for macOS
Keeps macOS awake and prevents display sleep while job automation pipelines are running.
"""

import os
import sys
import json
import atexit
import subprocess

class MacKeepAwake:
    """
    Context manager and controller to prevent macOS system and display sleep.
    Spawns `/usr/bin/caffeinate -disu -w <pid>`.
    
    Flags:
      -d: Prevent display from sleeping (keeps screen ON)
      -i: Prevent system from idle sleeping
      -s: Prevent system from sleeping on AC power
      -u: Assert user activity (keeps display active)
      -w <pid>: Automatically release assertions when process <pid> exits
    """
    def __init__(self, keep_screen_on=True, prevent_system_sleep=True):
        self.keep_screen_on = keep_screen_on
        self.prevent_system_sleep = prevent_system_sleep
        self.proc = None

    def start(self):
        if sys.platform != 'darwin' or self.proc is not None:
            return
        flags = []
        if self.keep_screen_on:
            flags.extend(['-d', '-u'])
        if self.prevent_system_sleep:
            flags.extend(['-i', '-s'])
        if not flags:
            flags = ['-i']

        cmd = ['/usr/bin/caffeinate'] + flags + ['-w', str(os.getpid())]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            print(f"[Power Management] ⚡ macOS keep-awake active (Screen ON: {self.keep_screen_on}, System awake: {self.prevent_system_sleep})")
            atexit.register(self.stop)
        except Exception as e:
            print(f"[Power Management] Notice: Could not engage caffeinate: {e}")

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                pass
            self.proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


def should_display_notification(is_urgent=False):
    """
    Determine if a macOS notification banner should be displayed.
    In 'quiet_mode', routine per-job notifications are suppressed
    to prevent interrupting the user, while urgent/completion notices still display.
    """
    try:
        cfg_path = os.path.expanduser('~/Documents/JobAutomation/config.json')
        if os.path.exists(cfg_path):
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            notif_cfg = cfg.get('notifications', {})
            if not notif_cfg.get('enabled', True):
                return False
            if notif_cfg.get('quiet_mode', True) and not is_urgent:
                return False
    except Exception:
        pass
    return True


def notify_macos(title, message, is_urgent=False):
    """
    Display a non-blocking macOS notification banner.
    Respects quiet_mode in config.json.
    """
    if sys.platform != 'darwin':
        return
    if not should_display_notification(is_urgent=is_urgent):
        return
    try:
        safe_title = title.replace('"', '\\"')
        safe_msg = message.replace('"', '\\"')
        subprocess.run(
            ['osascript', '-e', f'display notification "{safe_msg}" with title "{safe_title}"'],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4
        )
    except Exception:
        pass
