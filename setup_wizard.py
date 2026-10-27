#!/usr/bin/env python3
"""
setup_wizard.py — Automated Setup and Onboarding Wizard for JobAutomation
Initializes configuration, verifies system dependencies, and prepares directories.
"""

import os
import sys
import shutil
import json
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

def print_header(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)

def check_python_version():
    print("[1/5] Checking Python environment...")
    major, minor = sys.version_info.major, sys.version_info.minor
    if major < 3 or (major == 3 and minor < 9):
        print(f"❌ Error: Python 3.9 or higher is required. Found: Python {major}.{minor}")
        sys.exit(1)
    print(f"✅ Python {major}.{minor}.{sys.version_info.micro} detected.")

def check_dependencies():
    print("\n[2/5] Checking required Python dependencies...")
    requirements_file = BASE_DIR / "requirements.txt"
    if not requirements_file.exists():
        print("⚠️ requirements.txt not found. Skipping dependency check.")
        return

    missing = []
    packages = [
        ("playwright", "playwright"),
        ("requests", "requests"),
        ("reportlab", "reportlab"),
        ("pypdf", "pypdf"),
        ("fitz", "PyMuPDF"),
        ("PIL", "Pillow"),
        ("pytesseract", "pytesseract"),
    ]
    for mod_name, pkg_name in packages:
        try:
            __import__(mod_name)
        except ImportError:
            missing.append(pkg_name)

    if missing:
        print(f"⚠️ Missing packages: {', '.join(missing)}")
        install = input("Would you like to install them now? (y/n): ").strip().lower()
        if install == 'y':
            print("Installing dependencies...")
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(requirements_file)], check=True)
            print("✅ Dependencies installed.")
            print("Installing Playwright Chromium browser binary...")
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
            print("✅ Playwright browser installed.")
        else:
            print("Please run: pip install -r requirements.txt && playwright install chromium")
    else:
        print("✅ All core Python dependencies are installed.")

def initialize_directories():
    print("\n[3/5] Initializing local runtime directories...")
    directories = [
        BASE_DIR / "logs",
        BASE_DIR / "output",
        BASE_DIR / "dropzone",
        BASE_DIR / "browser_profile",
        BASE_DIR / "data",
        BASE_DIR / "templates"
    ]
    for d in directories:
        d.mkdir(parents=True, exist_ok=True)
    print("✅ Runtime folders verified.")

def initialize_configurations():
    print("\n[4/5] Checking configuration files...")
    config_file = BASE_DIR / "config.json"
    example_config = BASE_DIR / "config.example.json"
    
    if not config_file.exists():
        if example_config.exists():
            shutil.copy(example_config, config_file)
            print("✅ Created config.json from config.example.json.")
            print("   👉 Open config.json to fill in your personal search preferences & credentials.")
        else:
            print("⚠️ config.example.json not found.")
    else:
        print("✅ Existing config.json detected.")

    master_resume = BASE_DIR / "data" / "master_resume.json"
    example_resume = BASE_DIR / "data" / "master_resume.example.json"
    if not master_resume.exists():
        if example_resume.exists():
            shutil.copy(example_resume, master_resume)
            print("✅ Created data/master_resume.json from template.")
            print("   👉 Edit data/master_resume.json with your experience and skills.")
    else:
        print("✅ Existing data/master_resume.json detected.")

def final_summary():
    print_header("Setup Complete!")
    print("""
Your JobAutomation engine is ready.

Next steps:
1. Edit config.json to customize your target roles, locations, and email settings.
2. Edit data/master_resume.json to include your career experience.
3. Start the dashboard or run tests:
   • Launch Web Dashboard:   python3 dashboard.py
   • Run Unified CLI:        python3 run_all.py --status
   • Dry-Run Application:    python3 run_all.py --dry-run
""")

if __name__ == "__main__":
    print_header("JobAutomation Setup & Onboarding Wizard")
    check_python_version()
    check_dependencies()
    initialize_directories()
    initialize_configurations()
    final_summary()
