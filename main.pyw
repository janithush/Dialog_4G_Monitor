import json
import os
import sys
import time


def _ensure_single_instance():
    """Exit immediately if another copy is already running.

    Kills duplicate-writer races (double-clicks, editor F5, Startup +
    manual launch, launcher twin processes). Must run before any
    third-party imports so duplicates die in milliseconds.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(
            None, False, "Global\\DialogMonitorWidgetSingleInstance")
        # Keep a reference alive for the process lifetime.
        _ensure_single_instance._handle = handle
        if not handle or kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            print("[!] Another instance is already running. Exiting.")
            os._exit(0)
    except Exception:
        pass  # Never block startup on the guard itself.


_ensure_single_instance()

import requests
import subprocess
from playwright.sync_api import sync_playwright

# Anchor all relative files to script folder so Startup/headless runs work
# even when CWD is System32 or elsewhere.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.json")
DATA_FILE = os.path.join(BASE_DIR, "data.json")
LOCAL_INC = os.path.join(BASE_DIR, "variables.inc")
# Direct write into the skin folder (user approved). Keep local copy too.
SKIN_INC = os.path.join(
    os.path.expanduser("~"),
    "Documents", "Rainmeter", "Skins", "DialogMonitor", "variables.inc",
)
MAX_SLOTS = 5
RAINMETER_EXE = r"C:\Program Files\Rainmeter\Rainmeter.exe"
LOGIN_URL = "https://dialog.lk/mydialog-web/login"
API_URL = "https://dialog.lk/selfcare-proxy?r=usage/get"


def sanitize_inc(value):
    """Make a value safe for a Rainmeter .inc line: no newlines, no CR."""
    if value is None:
        return ""
    s = str(value).replace("\r", " ").replace("\n", " ").strip()
    # Leading semicolon would be parsed as a comment in .inc, so guard it.
    if s.startswith(";"):
        s = " " + s
    return s


def write_variables_inc(packages, status, updated_at):
    """Write Rainmeter-friendly variables.inc (no JSON/RegEx needed)."""
    lines = ["[Variables]"]
    lines.append(f"Status={sanitize_inc(status)}")
    lines.append(f"UpdatedAt={sanitize_inc(updated_at)}")
    lines.append(f"PkgCount={len(packages)}")

    for i in range(1, MAX_SLOTS + 1):
        if i <= len(packages):
            pkg = packages[i - 1]
            name = sanitize_inc(pkg.get("name", ""))
            remaining = sanitize_inc(pkg.get("remaining", ""))
            total = sanitize_inc(pkg.get("total", ""))
            expires = sanitize_inc(pkg.get("expires", ""))
            # Pre-format combined data string so the skin stays dumb.
            # e.g. "76.71 GB / 80 GB Total" or just "24.42 GB" when total empty.
            if total and total.upper() != "N/A":
                data_line = f"{remaining} / {total}" if remaining else total
            else:
                data_line = remaining
        else:
            name = remaining = total = expires = data_line = ""
        lines.append(f"Pkg{i}Name={name}")
        lines.append(f"Pkg{i}Data={sanitize_inc(data_line)}")
        lines.append(f"Pkg{i}Total={total}")
        lines.append(f"Pkg{i}Expires={expires}")

    content = "\n".join(lines) + "\n"

    for path in (LOCAL_INC, SKIN_INC):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # UTF-16 LE (with BOM) is Rainmeter's native encoding
            # (Rainmeter.ini itself is UTF-16). Must match monitor.ini.
            # Atomic write (tmp + replace) so readers never see torn files.
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-16") as f:
                f.write(content)
            os.replace(tmp, path)
        except Exception as e:
            print(f"[!] Could not write {path}: {e}")

    # Nudge Rainmeter to reload the new variables immediately.
    # Skin also auto-refreshes every 5 min as a fallback, so failure here is non-fatal.
    try:
        if os.path.exists(RAINMETER_EXE):
            subprocess.run(
                [RAINMETER_EXE, "!Refresh", "DialogMonitor"],
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass


def get_fresh_cookies_auto():
    print("[*] Launching browser for login...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto(LOGIN_URL)

        try:
            # Wait until login is fully verified and reaches dashboard
            page.wait_for_url("**/mydialog-web/**", timeout=120000)
            page.wait_for_load_state("networkidle")
            time.sleep(3)
        except Exception as e:
            print(f"[!] Login error or timeout: {e}")
            browser.close()
            return ""

        cookies = context.cookies()
        browser.close()

        cookie_dict = {c["name"]: c["value"] for c in cookies}
        cookie_string = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])

        with open(COOKIE_FILE, "w") as f:
            json.dump({"cookie": cookie_string}, f)

        print("[+] Logged in successfully. Cookies saved.")
        return cookie_string


def load_cookies():
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "r") as f:
                data = json.load(f)
                cookie_val = data.get("cookie", "")
                if cookie_val:
                    return cookie_val
        except Exception:
            pass
    return get_fresh_cookies_auto()


def fetch_data(cookie_header):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'lang': 'en',
        'uuid': 'e8720ecf-3694-4ef1-9770-f5b0e2d1a462',
        'msisdn': '337102988',
        'Origin': 'https://dialog.lk',
        'Referer': 'https://dialog.lk/mydialog-web/services/data-detail-tab',
        'Cookie': cookie_header
    }

    # If your Postman request required a body, put that dict here. Otherwise empty {}
    payload = json.dumps({})

    try:
        res = requests.post(API_URL, headers=headers, data=payload, timeout=15)
        return res
    except Exception as e:
        print(f"[!] Request exception: {e}")
        return None


def extract_items_recursive(data_node):
    """Recursively search for items containing usage_name and remaining_amount"""
    found = []
    if isinstance(data_node, dict):
        if "usage_name" in data_node and "remaining_amount" in data_node:
            found.append(data_node)
        else:
            for v in data_node.values():
                found.extend(extract_items_recursive(v))
    elif isinstance(data_node, list):
        for item in data_node:
            found.extend(extract_items_recursive(item))
    return found


def update_data_cycle():
    cookie = load_cookies()
    res = fetch_data(cookie)

    # Session expiration check
    is_expired = False
    if res is None or res.status_code in [401, 403]:
        is_expired = True
    elif res.status_code == 200:
        try:
            body = res.json()
            if body.get("code") in [401, 404] or body.get("success") is False:
                is_expired = True
        except Exception:
            is_expired = True

    if is_expired:
        print("[!] Session expired. Triggering login...")
        cookie = get_fresh_cookies_auto()
        res = fetch_data(cookie)

    if res and res.status_code == 200:
        res_json = res.json()
        
        # Recursive extractor finds packages regardless of JSON depth
        all_raw_items = extract_items_recursive(res_json.get("data", {}))

        output_packages = []
        for item in all_raw_items:
            rem_amount = item.get("remaining_amount", "")

            # Filter out expired / 0 Bytes packages
            if "0 Bytes" in rem_amount or not rem_amount:
                continue

            pkg_name = item.get("usage_name", "N/A")
            exp_time = item.get("expiry", "N/A")
            total_amount = item.get("total", "N/A")

            output_packages.append({
                "name": pkg_name,
                "remaining": rem_amount,
                "total": total_amount,
                "expires": exp_time
            })

        # Save to data.json for Rainmeter (atomic so readers never see torn JSON)
        updated_at = time.strftime("%H:%M:%S")
        _data_tmp = DATA_FILE + ".tmp"
        with open(_data_tmp, "w", encoding="utf-8") as f:
            json.dump({
                "status": "success",
                "updated_at": updated_at,
                "packages": output_packages
            }, f)
        os.replace(_data_tmp, DATA_FILE)

        # Save Rainmeter-friendly variables.inc (primary display feed)
        write_variables_inc(output_packages, "success", updated_at)

        print(f"[+] Successfully extracted {len(output_packages)} active packages.")
    else:
        status = res.status_code if res else "No Response"
        print(f"[!] Fetch failed with status: {status}")
        # Write error state so the widget shows a message instead of stale blanks.
        try:
            write_variables_inc([], "error", time.strftime("%H:%M:%S"))
        except Exception as e:
            print(f"[!] Could not write error state: {e}")


if __name__ == "__main__":
    while True:
        update_data_cycle()
        time.sleep(300)  # Updates every 5 minutes