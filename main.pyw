import json
import os
import sys
import time


def _acquire_single_instance_mutex():
    """Create (or open) the global single-instance mutex.

    Returns True if another instance was already running
    (GetLastError() == ERROR_ALREADY_EXISTS), False otherwise.
    Keeps the handle alive in a module global for process lifetime.
    Never raises — returns False on any failure so startup is never blocked.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(
            None, False, "Global\\DialogMonitorWidgetSingleInstance")
        # Keep a reference alive for the process lifetime.
        _acquire_single_instance_mutex._handle = handle
        global _MUTEX_HANDLE
        _MUTEX_HANDLE = handle
        if not handle:
            return False
        return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False


# CLI flag for on-demand manual sync (Rainmeter double-click action).
# Parsed before the mutex guard so interactive runs can coexist with the daemon.
INTERACTIVE_LOGIN = "--interactive-login" in sys.argv
SHOW_HELP = "--help" in sys.argv or "-h" in sys.argv
# --help must never be blocked by the mutex either (read-only, no writes).
BYPASS_SINGLE_INSTANCE_EXIT = INTERACTIVE_LOGIN or SHOW_HELP
ANOTHER_INSTANCE_RUNNING = False
_MUTEX_HANDLE = None


def _ensure_single_instance():
    """Exit immediately if another copy is already running.

    Kills duplicate-writer races (double-clicks, editor F5, Startup +
    manual launch, launcher twin processes). Must run before any
    third-party imports so duplicates die in milliseconds.

    Exception: `--interactive-login` (on-demand manual sync) deliberately
    does NOT exit when the daemon is already running. It logs the overlap
    and continues as a short-lived one-shot (headed login -> fetch ->
    write variables.inc -> !Refresh -> exit) so the user can force a
    re-login/sync via double-click without killing the background daemon.
    Atomic tmp+replace writes keep the brief overlap tear-free.
    """
    global ANOTHER_INSTANCE_RUNNING
    ANOTHER_INSTANCE_RUNNING = _acquire_single_instance_mutex()
    if ANOTHER_INSTANCE_RUNNING and not BYPASS_SINGLE_INSTANCE_EXIT:
        print("[!] Another instance is already running. Exiting.")
        os._exit(0)
    if ANOTHER_INSTANCE_RUNNING and INTERACTIVE_LOGIN:
        print("[*] Daemon already running — "
              "interactive sync will run one-shot alongside it.")


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
ENV_FILE = os.path.join(BASE_DIR, ".env")


def _load_dotenv(path=ENV_FILE):
    """Minimal .env loader (no third-party dependency).

    Loads KEY=VALUE lines into os.environ (without overriding existing
    env vars). Supports quoted values and ignores comments/blank lines.
    """
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception as e:
        print(f"[!] Could not load {path}: {e}")


_load_dotenv()

# Dialog identity / polling — configurable via .env, fallback to legacy defaults.
DIALOG_MSISDN = os.environ.get("DIALOG_MSISDN", "337102988")
DIALOG_UUID = os.environ.get(
    "DIALOG_UUID", "e8720ecf-3694-4ef1-9770-f5b0e2d1a462")
DIALOG_LANG = os.environ.get("DIALOG_LANG", "en")
try:
    POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))
except ValueError:
    POLL_INTERVAL = 300


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


def save_session_credentials(cookie_string, bearer_tokens=None):
    """Persist captured session to cookies.json AND .env (atomic writes).

    - cookies.json: {"cookie": "...", "bearer_tokens": {...}} (legacy readers
      only need "cookie"; extra keys are ignored).
    - .env: upserts DIALOG_COOKIE and DIALOG_BEARER_TOKEN while preserving
      all other keys/comments. Created with a header if missing.
    Never raises — failures are logged only.
    """
    bearer_tokens = bearer_tokens or {}
    # 1) cookies.json (atomic, UTF-8)
    try:
        payload = {"cookie": cookie_string}
        if bearer_tokens:
            payload["bearer_tokens"] = bearer_tokens
        tmp = COOKIE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, COOKIE_FILE)
        print("[+] Session saved to cookies.json.")
    except Exception as e:
        print(f"[!] Could not save {COOKIE_FILE}: {e}")
    # 2) .env upsert (atomic, UTF-8, preserve existing keys/comments)
    try:
        existing_lines = []
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                existing_lines = f.read().splitlines()
        # Drop old credential lines; re-append fresh ones at the end.
        cred_keys = ("DIALOG_COOKIE=", "DIALOG_BEARER_TOKEN=")
        kept = [ln for ln in existing_lines
                if not ln.strip().startswith(cred_keys)]
        if not kept and not existing_lines:
            kept = ["# DialogMonitor credentials — keep secret, never commit."]
        kept.append(f'DIALOG_COOKIE="{cookie_string}"')
        token_val = ""
        for search in ("access_token", "accessToken", "bearer",
                       "id_token", "idToken", "token"):
            if search in bearer_tokens and bearer_tokens[search]:
                token_val = str(bearer_tokens[search])
                break
        if not token_val and bearer_tokens:
            # Fall back to first captured value.
            token_val = str(next(iter(bearer_tokens.values())))
        if token_val:
            kept.append(f'DIALOG_BEARER_TOKEN="{token_val}"')
            os.environ["DIALOG_BEARER_TOKEN"] = token_val
        os.environ["DIALOG_COOKIE"] = cookie_string
        content = "\n".join(kept) + "\n"
        tmp_env = ENV_FILE + ".tmp"
        with open(tmp_env, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_env, ENV_FILE)
        print(f"[+] Credentials saved to {ENV_FILE}.")
    except Exception as e:
        print(f"[!] Could not save {ENV_FILE}: {e}")


def _capture_session_tokens(context, page):
    """Build cookie header + best-effort bearer-token dict from a session.

    - Always captures context.cookies() -> "a=b; c=d" header.
    - Best-effort scans localStorage/sessionStorage for token-like keys
      (token/bearer/jwt/auth/access/id_token) without ever raising.
    Returns (cookie_string, bearer_tokens_dict).
    """
    cookies = []
    try:
        cookies = context.cookies()
    except Exception as e:
        print(f"[!] Could not read cookies: {e}")
    cookie_dict = {c.get("name"): c.get("value")
                   for c in cookies if c.get("name")}
    cookie_string = "; ".join(
        f"{k}={v}" for k, v in cookie_dict.items() if v is not None)

    bearer_tokens = {}
    # Cookie names that often carry tokens — surface them too.
    try:
        for name, val in cookie_dict.items():
            low = (name or "").lower()
            if any(k in low for k in ("bearer", "token", "jwt", "auth")) and val:
                bearer_tokens[name] = val
    except Exception:
        pass
    # Web-storage scan (SPA bearer tokens often live here, not in cookies).
    for storage in ("localStorage", "sessionStorage"):
        try:
            data = page.evaluate(
                f"() => {{ const o = {{}}; try {{ "
                f"for (let i = 0; i < {storage}.length; i++) {{ "
                f"const k = {storage}.key(i); "
                f"try {{ o[k] = {storage}.getItem(k); }} catch(e) {{}} }} "
                f"}} catch(e) {{}} return o; }}")
            if isinstance(data, dict):
                for k, v in data.items():
                    if not isinstance(v, str) or not v:
                        continue
                    low = k.lower()
                    if any(t in low for t in ("token", "bearer", "jwt",
                                              "auth", "access", "id_token")):
                        # Avoid stuffing huge blobs; cap at 8k chars.
                        bearer_tokens[f"{storage}.{k}"] = v[:8000]
        except Exception:
            continue
    return cookie_string, bearer_tokens


def _login_via_headed_browser(interactive=False, timeout_ms=120000):
    """Headed Playwright login; returns fresh cookie header or "".

    Always uses headless=False so the Dialog login page + OTP prompt are
    visible. Waits for the dashboard (mydialog-web/**), captures cookies +
    bearer tokens, persists via save_session_credentials(), and closes the
    browser in a finally block so no Chromium/Playwright child processes
    are left behind (the `with sync_playwright()` block tears down the
    driver even on user-close/timeout).
    """
    mode = "interactive login" if interactive else "login"
    print(f"[*] Launching browser for {mode} (headed, OTP visible)...")
    if interactive:
        print("[*] Please complete login + OTP in the opened browser window. "
              "Waiting for dashboard...")
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto(LOGIN_URL)
            except Exception as e:
                print(f"[!] Could not open login page: {e}")
                return ""
            try:
                # Wait until login/OTP is verified and dashboard is reached.
                page.wait_for_url("**/mydialog-web/**", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=30000)
                except Exception:
                    pass  # Settle is best-effort; cookies are what matter.
                time.sleep(3)
            except Exception as e:
                print(f"[!] Login error, timeout, or window closed: {e}")
                return ""
            cookie_string, bearer_tokens = _capture_session_tokens(
                context, page)
            if not cookie_string:
                print("[!] Login completed but no cookies were captured.")
                return ""
            save_session_credentials(cookie_string, bearer_tokens)
            if bearer_tokens:
                print(f"[+] Captured {len(bearer_tokens)} token(s) "
                      f"alongside cookies.")
            print("[+] Logged in successfully. Cookies saved.")
            return cookie_string
    except Exception as e:
        print(f"[!] Browser session failed: {e}")
        return ""
    finally:
        # Clean shutdown: no lingering Chromium children.
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass


def get_fresh_cookies_auto():
    """Automatic re-login path (session expiry). Headed so SSO/OTP works."""
    return _login_via_headed_browser(interactive=False, timeout_ms=120000)


def get_fresh_cookies_interactive():
    """Manual on-demand login via `main.pyw --interactive-login`.

    Opens a visible browser for the user to finish login/OTP, captures the
    active session cookies/bearer tokens, and persists them to cookies.json
    + .env. Browser is always closed cleanly afterwards.
    """
    print("[*] Manual sync requested (--interactive-login).")
    if ANOTHER_INSTANCE_RUNNING:
        # Mutex already showed the daemon is alive; we intentionally continue
        # as a short-lived one-shot instead of crashing/exiting.
        print("[*] Background daemon detected — proceeding one-shot; "
              "daemon keeps running.")
    else:
        print("[*] No background daemon detected — this run will sync once "
              "and can keep running as the daemon afterwards.")
    return _login_via_headed_browser(interactive=True, timeout_ms=180000)


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
    # Identity headers are .env-configurable (DIALOG_MSISDN/UUID/LANG).
    # Falls back to legacy defaults so existing installs keep working.
    # A captured bearer token (if any) is sent as Authorization as well —
    # harmless when the backend only checks cookies.
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'lang': DIALOG_LANG,
        'uuid': DIALOG_UUID,
        'msisdn': DIALOG_MSISDN,
        'Origin': 'https://dialog.lk',
        'Referer': 'https://dialog.lk/mydialog-web/services/data-detail-tab',
        'Cookie': cookie_header
    }
    bearer = os.environ.get("DIALOG_BEARER_TOKEN", "")
    if bearer:
        headers["Authorization"] = (
            bearer if bearer.lower().startswith("bearer ")
            else f"Bearer {bearer}")

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


def update_data_cycle(cookie_override=None):
    """One fetch -> parse -> atomic write -> !Refresh cycle.

    Args:
        cookie_override: when provided (non-empty), this fresh cookie header
            is used directly instead of load_cookies(). Used by the manual
            --interactive-login one-shot so the just-captured session is
            fetched immediately without a redundant disk read.

    Returns True on success, False on fetch failure.
    """
    if cookie_override:
        cookie = cookie_override
    else:
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
        updated_at = time.strftime("%I:%M:%S %p")
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
        return True
    else:
        status = res.status_code if res else "No Response"
        print(f"[!] Fetch failed with status: {status}")
        # Write error state so the widget shows a message instead of stale blanks.
        try:
            write_variables_inc([], "error", time.strftime("%I:%M:%S %p"))
        except Exception as e:
            print(f"[!] Could not write error state: {e}")
        return False


def run_interactive_login_once():
    """On-demand manual sync for `--interactive-login` (double-click action).

    1. Headed browser login (OTP visible) -> capture cookies/bearer tokens.
    2. Persist to cookies.json + .env.
    3. Immediate usage fetch -> clean metrics -> variables.inc -> !Refresh.
    4. Browser always closed cleanly; no child processes left behind.
    Returns True on full success, False otherwise.
    """
    print("[*] Starting on-demand interactive sync...")
    cookie = get_fresh_cookies_interactive()
    if not cookie:
        print("[!] Interactive login did not produce a session. "
              "Leaving current widget data untouched.")
        return False
    print("[*] Session captured — fetching usage immediately...")
    # write_variables_inc() inside update_data_cycle triggers
    # `Rainmeter.exe !Refresh DialogMonitor` on success.
    ok = update_data_cycle(cookie_override=cookie)
    if ok:
        print("[+] Manual sync complete — widget refreshed.")
    else:
        print("[!] Manual sync login succeeded but usage fetch failed.")
    return ok


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage:")
        print("  main.pyw                  Run background daemon (5-min loop).")
        print("  main.pyw --interactive-login  "
              "On-demand headed login + immediate sync + skin refresh.")
        sys.exit(0)
    if INTERACTIVE_LOGIN:
        ok = run_interactive_login_once()
        if ANOTHER_INSTANCE_RUNNING:
            # Daemon owns the loop — exit one-shot with status code.
            sys.exit(0 if ok else 1)
        # No daemon was running: stay alive as the daemon so the widget
        # keeps refreshing every POLL_INTERVAL.
        print("[*] No daemon was running — continuing as background daemon.")
        if not ok:
            # Still enter loop; next cycle retries automatically.
            pass
        while True:
            update_data_cycle()
            time.sleep(POLL_INTERVAL)
    else:
        while True:
            update_data_cycle()
            time.sleep(POLL_INTERVAL)  # Updates every 5 minutes by default