import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler


# Anchor all relative files to script folder so Startup/headless runs work
# even when CWD is System32 or elsewhere. Defined early so the
# single-instance guard can signal via the trigger file BEFORE heavy
# third-party imports (keeps the signal-and-exit path at ~0.1s).
BASE_DIR_EARLY = os.path.dirname(os.path.abspath(__file__))
TRIGGER_FILE_EARLY = os.path.join(BASE_DIR_EARLY, ".refresh.trigger")
PAUSE_FILE_EARLY = os.path.join(BASE_DIR_EARLY, ".paused")
LOG_FILE_EARLY = os.path.join(BASE_DIR_EARLY, "daemon.log")
# Daemon trigger poll cadence (seconds). The daemon wakes every 1s to
# consume refresh requests; periodic usage fetches still honor POLL_INTERVAL.
TRIGGER_POLL_SECONDS = 1.0


def _setup_logger():
    """Bootstrap stdlib logging to a rotating daemon.log.

    RotatingFileHandler: maxBytes=1048576 (1 MB), backupCount=1 (per spec).
    Under pythonw.exe sys.stdout/sys.stderr are None; in that case we attach
    no stream handler and log exclusively to disk, so production runs are
    fully observable. Never raises - logging failure must not block startup.
    """
    logger = logging.getLogger("dialogmonitor")
    if logger.handlers:
        return logger  # idempotent for module re-eval edge cases
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    try:
        fh = RotatingFileHandler(
            LOG_FILE_EARLY, maxBytes=1_048_576, backupCount=1, encoding="utf-8")
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except Exception:
        pass
    stream = sys.stdout or sys.stderr
    if stream:
        sh = logging.StreamHandler(stream)
        sh.setFormatter(formatter)
        logger.addHandler(sh)
    return logger


log = _setup_logger()


def _signal_daemon_refresh():
    """Touch the refresh trigger file so a running daemon fetches ASAP.

    Never raises - failures are logged only. Fast by design (~ms) so a
    manual `--refresh` alongside a live daemon exits in ~0.1s.
    """
    try:
        with open(TRIGGER_FILE_EARLY, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S"))
        return True
    except Exception as e:
        print(f"[!] Could not signal daemon refresh: {e}")
        log.error("Could not signal daemon refresh: %s", e)
        return False


def _set_pause_state(pause):
    """Create or remove the `.paused` pause flag file. Never raises.

    Payload is a timestamp for debuggability; only file existence matters.
    """
    try:
        if pause:
            with open(PAUSE_FILE_EARLY, "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%dT%H:%M:%S"))
        elif os.path.exists(PAUSE_FILE_EARLY):
            os.remove(PAUSE_FILE_EARLY)
        return True
    except Exception as e:
        print(f"[!] Could not update pause flag: {e}")
        log.error("Could not %s pause flag: %s",
                  "create" if pause else "remove", e)
        return False


def _acquire_single_instance_mutex():
    """Create (or open) the global single-instance mutex.

    Returns True if another instance was already running
    (GetLastError() == ERROR_ALREADY_EXISTS), False otherwise.
    Keeps the handle alive in a module global for process lifetime.
    Never raises - returns False on any failure so startup is never blocked.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Explicit signatures: without restype/argtypes ctypes assumes a
        # 32-bit c_int return, truncating 64-bit HANDLE values (audit #7).
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
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


# CLI flags for on-demand manual sync (Rainmeter double-click action).
# Parsed before the mutex guard so interactive / refresh / pause runs can
# coexist with the daemon.
INTERACTIVE_LOGIN = "--interactive-login" in sys.argv
REFRESH_REQUESTED = "--refresh" in sys.argv
TOGGLE_PAUSE_REQUESTED = "--toggle-pause" in sys.argv
PAUSE_REQUESTED = "--pause" in sys.argv
RESUME_REQUESTED = "--resume" in sys.argv
SHOW_HELP = "--help" in sys.argv or "-h" in sys.argv

# --help / --refresh / --interactive-login / pause modes must never be
# blocked by the mutex without handling: --refresh signals the daemon and
# exits fast when one is running, otherwise it auto-promotes to the daemon
# (see __main__). Pause modes flip the flag and signal the live daemon via
# the same pre-import fast path.
BYPASS_SINGLE_INSTANCE_EXIT = (
    INTERACTIVE_LOGIN or REFRESH_REQUESTED or SHOW_HELP
    or TOGGLE_PAUSE_REQUESTED or PAUSE_REQUESTED or RESUME_REQUESTED)
ANOTHER_INSTANCE_RUNNING = False
_MUTEX_HANDLE = None


def _handle_pause_request():
    """Shared fast path for --toggle-pause / --pause / --resume.

    Flips the `.paused` flag in ~1ms, then touches `.refresh.trigger` so a
    live daemon applies the change within ~1s (its 1s poll consumes the
    trigger and rewrites the display state). No heavy imports, no network.
    """
    if TOGGLE_PAUSE_REQUESTED:
        desired = not os.path.exists(PAUSE_FILE_EARLY)
    elif PAUSE_REQUESTED:
        desired = True
    else:  # RESUME_REQUESTED
        desired = False
    if not _set_pause_state(desired):
        return False
    if ANOTHER_INSTANCE_RUNNING:
        _signal_daemon_refresh()
    verb = "Paused" if desired else "Resumed"
    note = ("daemon running - notified"
            if ANOTHER_INSTANCE_RUNNING
            else "no daemon running - flag applies at next daemon start")
    print(f"[*] {verb} polling ({note}).")
    log.info("%s: .paused %s; %s", verb,
             "created" if desired else "removed", note)
    return True


def _ensure_single_instance():
    """Exit immediately if another copy is already running.

    Kills duplicate-writer races (double-clicks, editor F5, Startup +
    manual launch, launcher twin processes). Must run before any
    third-party imports so duplicates die in milliseconds.

    Exception: `--interactive-login` (on-demand manual sync) deliberately
    does NOT exit when the daemon is already running. It logs the overlap
    and continues as a short-lived one-shot (headed login -> fetch ->
    write variables.inc -> !Refresh -> exit) so the user can force a
    re-login/sync via middle-click without killing the background daemon.
    Atomic tmp+replace writes keep the brief overlap tear-free.

    Exception: `--refresh` (lightweight manual sync) NEVER duplicates work.
    If a daemon is already running it touches `.refresh.trigger` and exits
    in ~0.1s (before heavy imports); the daemon picks it up on its 1s poll
    and fetches immediately. Only when NO daemon is running does this
    process continue - it then auto-promotes into the daemon loop.

    Exception: `--toggle-pause` / `--pause` / `--resume` follow the same
    pre-import fast path: flip the `.paused` flag (~1ms), touch
    `.refresh.trigger`, exit.
    """
    global ANOTHER_INSTANCE_RUNNING
    ANOTHER_INSTANCE_RUNNING = _acquire_single_instance_mutex()
    if ANOTHER_INSTANCE_RUNNING and REFRESH_REQUESTED and not INTERACTIVE_LOGIN:
        # Fast path: signal the live daemon, exit before importing
        # requests/playwright so double-click feels instant.
        print("[*] Daemon already running - refresh requested.")
        _signal_daemon_refresh()
        os._exit(0)
    if (ANOTHER_INSTANCE_RUNNING
            and (TOGGLE_PAUSE_REQUESTED or PAUSE_REQUESTED or RESUME_REQUESTED)
            and not (INTERACTIVE_LOGIN or REFRESH_REQUESTED)):
        # Same pre-import fast path: flip the flag (~1ms), signal the
        # daemon's 1s poll, exit. No heavy imports.
        print("[*] Daemon already running - pause toggle requested.")
        _handle_pause_request()
        os._exit(0)
    if ANOTHER_INSTANCE_RUNNING and not BYPASS_SINGLE_INSTANCE_EXIT:
        print("[!] Another instance is already running. Exiting.")
        os._exit(0)
    if ANOTHER_INSTANCE_RUNNING and INTERACTIVE_LOGIN:
        print("[*] Daemon already running - "
              "interactive sync will run one-shot alongside it.")
    if ANOTHER_INSTANCE_RUNNING and REFRESH_REQUESTED and INTERACTIVE_LOGIN:
        # Explicit headed re-login wins over lightweight signal; the
        # interactive one-shot below handles it.
        pass


_ensure_single_instance()

import requests
import subprocess
from playwright.sync_api import sync_playwright

# Anchor all relative files to script folder so Startup/headless runs work
# even when CWD is System32 or elsewhere. (BASE_DIR_EARLY above holds the
# same value for pre-import use; reuse it here to keep a single source.)
BASE_DIR = BASE_DIR_EARLY
TRIGGER_FILE = TRIGGER_FILE_EARLY
PAUSE_FILE = PAUSE_FILE_EARLY
LOG_FILE = LOG_FILE_EARLY
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
        log.error("Could not load %s: %s", path, e)


_load_dotenv()

# Dialog identity - hardcoded fallbacks REMOVED (audit finding #1, spec E):
# MSISDN + UUID were real personal PII committed to the repo. .env is now
# mandatory; _validate_identity() exits cleanly with Status=config_error
# if these keys are missing.
DIALOG_MSISDN = os.environ.get("DIALOG_MSISDN", "")
DIALOG_UUID = os.environ.get("DIALOG_UUID", "")
DIALOG_LANG = os.environ.get("DIALOG_LANG", "en")
try:
    POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "900"))
except ValueError:
    POLL_INTERVAL = 900

# Passive-auth switch (spec C): when falsy (default) the daemon NEVER
# launches Chromium on its own - session expiry degrades the widget to
# Status=expired and the browser appears only via --interactive-login
# (widget middle-click).
AUTO_RELOGIN = str(os.environ.get("AUTO_RELOGIN", "0")).strip().lower() in (
    "1", "true", "yes", "on")

# ---- In-memory display-state cache (spec A) ------------------------------
# Kept so pause toggling can rewrite variables.inc without network I/O.
LAST_PACKAGES = None   # list of package dicts from last successful fetch
LAST_STATUS = "success"
_PAUSED = False        # mirrored from PAUSE_FILE inside the daemon loop

STATUS_MESSAGES = {
    "success": "",
    "error": "Update failed - retrying...",
    "expired": "Session expired - middle-click to re-login",
    "config_error": "Config error: set DIALOG_MSISDN/DIALOG_UUID in .env",
}


def _record_and_write(packages, status, updated_at=None):
    """Update the in-memory cache AND write the display feed (+ !Refresh)."""
    global LAST_PACKAGES, LAST_STATUS
    updated_at = updated_at or time.strftime("%I:%M:%S %p")
    LAST_PACKAGES = list(packages)
    LAST_STATUS = status
    write_variables_inc(packages, status, updated_at)


def _write_state_only():
    """Rewrite variables.inc from cache ONLY - no network, no re-login.

    Used on pause transitions so the skin reflects the new polling state
    immediately while keeping the last known package data on screen.
    """
    packages = LAST_PACKAGES or []
    log.debug("Writing state-only feed (status=%s, %d packages).",
              LAST_STATUS, len(packages))
    write_variables_inc(packages, LAST_STATUS, time.strftime("%I:%M:%S %p"))


def _refresh_pause_state():
    """Sync module state with the `.paused` flag on disk.

    Checked inside the existing 1s daemon iteration (zero extra wakeups);
    on a state transition, push the new state to the skin immediately via
    a cache-only write + !Refresh.
    """
    global _PAUSED
    on_disk = os.path.exists(PAUSE_FILE)
    if on_disk != _PAUSED:
        _PAUSED = on_disk
        log.info("Pause state changed -> %s",
                 "PAUSED" if on_disk else "RUNNING")
        try:
            _write_state_only()
        except Exception:
            log.exception("Failed to write pause state to skin")
    return _PAUSED


def _validate_identity():
    """Spec E: fail clean (no crash loop) when required .env keys are absent.

    Writes Status=config_error to the widget, logs a CRITICAL entry, and
    exits with code 2 before any network or browser attempt is made.
    """
    missing = [
        key for key, val in (
            ("DIALOG_MSISDN", DIALOG_MSISDN),
            ("DIALOG_UUID", DIALOG_UUID),
        ) if not val
    ]
    if missing:
        log.critical(
            "Missing required .env keys: %s. Set them and restart the daemon.",
            ", ".join(missing))
        try:
            write_variables_inc(
                [], "config_error", time.strftime("%I:%M:%S %p"))
        except Exception:
            log.exception("Could not write config_error state to skin")
        sys.exit(2)


def sanitize_inc(value):
    """Make a value safe for a Rainmeter .inc line: no newlines, no CR."""
    if value is None:
        return ""
    s = str(value).replace("\r", " ").replace("\n", " ").strip()
    # Leading semicolon would be parsed as a comment in .inc, so guard it.
    if s.startswith(";"):
        s = " " + s
    return s


def write_variables_inc(packages, status, updated_at, paused=None):
    """Write Rainmeter-friendly variables.inc (no JSON/RegEx needed).

    paused=None resolves from the filesystem so every write (fetch, error,
    state-only) embeds the CURRENT pause state - a fetch while paused can
    never erase Paused=1 from the feed.
    """
    if paused is None:
        paused = os.path.exists(PAUSE_FILE)
    lines = ["[Variables]"]
    lines.append(f"Status={sanitize_inc(status)}")
    lines.append(f"StatusMessage={sanitize_inc(STATUS_MESSAGES.get(status, ''))}")
    lines.append(f"UpdatedAt={sanitize_inc(updated_at)}")
    lines.append(f"Paused={1 if paused else 0}")
    lines.append(f"PausedGlyph={sanitize_inc('▶' if paused else '⏸')}")
    paused_tip = ('Paused - Click to Resume' if paused
                  else 'Running - Click to Pause')
    lines.append(f"PausedToolTip={sanitize_inc(paused_tip)}")
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

    # Machine-specific launcher targets generated by the daemon so the
    # skin stays portable (spec 2). Decision 1: explicitly resolve
    # pythonw.exe so widget clicks NEVER flash a console window, even if
    # the daemon was booted via console python.exe.
    python_exe = sys.executable
    if os.name == "nt":
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(pythonw):
            python_exe = pythonw
    lines.append(f"PYTHON_EXE={sanitize_inc(python_exe)}")
    lines.append(f"MAIN_SCRIPT={sanitize_inc(os.path.abspath(__file__))}")
    lines.append(f"BASE_DIR={sanitize_inc(BASE_DIR)}")

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
            log.error("Could not write %s: %s", path, e)

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
    - .env: upserts DIALOG_BEARER_TOKEN while preserving all other
      keys/comments. Created with a header if missing.
      DIALOG_COOKIE writes REMOVED (audit bug #5): the cookie header is only
      ever sourced from cookies.json (load_cookies()), so a second plaintext
      copy in .env added attack surface with zero functional benefit. The
      strip filter below remains so legacy installs get cleaned up on the
      next login.
    Never raises - failures are logged only.
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
        log.info("Session saved to cookies.json.")
    except Exception as e:
        print(f"[!] Could not save {COOKIE_FILE}: {e}")
        log.exception("Could not save %s", COOKIE_FILE)
    # 2) .env upsert (atomic, UTF-8, preserve existing keys/comments).
    try:
        existing_lines = []
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                existing_lines = f.read().splitlines()
        # Drop old credential lines; re-append fresh ones at the end.
        # DIALOG_COOKIE= stays in the strip filter only for legacy cleanup.
        cred_keys = ("DIALOG_COOKIE=", "DIALOG_BEARER_TOKEN=")
        kept = [ln for ln in existing_lines
                if not ln.strip().startswith(cred_keys)]
        if not kept and not existing_lines:
            kept = ["# DialogMonitor credentials - keep secret, never commit."]
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
        content = "\n".join(kept) + "\n"
        tmp_env = ENV_FILE + ".tmp"
        with open(tmp_env, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_env, ENV_FILE)
        print(f"[+] Credentials saved to {ENV_FILE}.")
        log.info("Credentials saved to %s.", ENV_FILE)
    except Exception as e:
        print(f"[!] Could not save {ENV_FILE}: {e}")
        log.exception("Could not save %s", ENV_FILE)


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
        log.warning("Could not read cookies: %s", e)
    cookie_dict = {c.get("name"): c.get("value")
                   for c in cookies if c.get("name")}
    cookie_string = "; ".join(
        f"{k}={v}" for k, v in cookie_dict.items() if v is not None)

    bearer_tokens = {}
    # Cookie names that often carry tokens - surface them too.
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

    Spec C: this function is ONLY reachable via --interactive-login or
    AUTO_RELOGIN=1; the default passive-auth daemon never calls it.
    """
    mode = "interactive login" if interactive else "login"
    print(f"[*] Launching browser for {mode} (headed, OTP visible)...")
    log.info("Launching browser for %s (headed, timeout=%sms).",
             mode, timeout_ms)
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
                log.exception("Could not open login page")
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
                log.warning("Login error, timeout, or window closed: %s", e)
                return ""
            cookie_string, bearer_tokens = _capture_session_tokens(
                context, page)
            if not cookie_string:
                print("[!] Login completed but no cookies were captured.")
                log.error("Login completed but no cookies were captured.")
                return ""
            save_session_credentials(cookie_string, bearer_tokens)
            if bearer_tokens:
                print(f"[+] Captured {len(bearer_tokens)} token(s) "
                      f"alongside cookies.")
                log.info("Captured %d bearer token(s) alongside cookies.",
                         len(bearer_tokens))
            print("[+] Logged in successfully. Cookies saved.")
            log.info("Login succeeded; session persisted.")
            return cookie_string
    except Exception as e:
        print(f"[!] Browser session failed: {e}")
        log.exception("Browser session failed")
        return ""
    finally:
        # Clean shutdown: no lingering Chromium children.
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass


def get_fresh_cookies_auto():
    """Automatic re-login path (ONLY used when AUTO_RELOGIN=1).

    Headed so SSO/OTP works; never called in the default passive mode.
    """
    return _login_via_headed_browser(interactive=False, timeout_ms=120000)


def get_fresh_cookies_interactive():
    """Manual on-demand login via `main.pyw --interactive-login`.

    Opens a visible browser for the user to finish login/OTP, captures the
    active session cookies/bearer tokens, and persists them to cookies.json
    + .env. Browser is always closed cleanly afterwards.
    """
    print("[*] Manual sync requested (--interactive-login).")
    log.info("Manual sync requested via --interactive-login.")
    if ANOTHER_INSTANCE_RUNNING:
        # Mutex already showed the daemon is alive; we intentionally continue
        # as a short-lived one-shot instead of crashing/exiting.
        print("[*] Background daemon detected - proceeding one-shot; "
              "daemon keeps running.")
        log.info("Daemon alive - interactive login runs as one-shot.")
    else:
        print("[*] No background daemon detected - this run will sync once "
              "and can keep running as the daemon afterwards.")
        log.info("No daemon alive - interactive login may auto-promote.")
    return _login_via_headed_browser(interactive=True, timeout_ms=180000)


def load_cookies():
    """Return the stored cookie header, or "" in passive mode.

    Spec C: with AUTO_RELOGIN=0 (default) a missing cookies.json NEVER
    spawns a browser - the fetch proceeds unauthenticated, the widget
    degrades to Status=expired, and the only path to Chromium is the
    explicit --interactive-login middle-click.
    """
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "r") as f:
                data = json.load(f)
                cookie_val = data.get("cookie", "")
                if cookie_val:
                    return cookie_val
        except Exception:
            log.exception("Could not read cookies.json - continuing without session.")
            return ""
    if AUTO_RELOGIN:
        log.info("No cookies.json - AUTO_RELOGIN=1: launching browser login.")
        return get_fresh_cookies_auto()
    log.info("No cookies.json present - passive mode: browser NOT launched. "
             "Widget will show expired state; use --interactive-login.")
    return ""


def fetch_data(cookie_header):
    # Identity headers are .env-configurable (DIALOG_MSISDN/UUID/LANG);
    # .env is mandatory since the PII fallbacks were removed.
    # A captured bearer token (if any) is sent as Authorization as well -
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
        log.exception("Request exception during usage fetch")
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


def _is_session_expired(res):
    """Classify a fetch response. Returns (is_expired, reason).

    JSON-parse failures are explicitly NOT proof of expiry (spec C: a parse
    error must never spawn Chromium, even when AUTO_RELOGIN=1); they fall
    through to the guarded success path and write Status=error instead.
    """
    if res is None:
        return True, "no-response"
    if res.status_code in (401, 403):
        return True, f"http-{res.status_code}"
    if res.status_code == 200:
        try:
            body = res.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            return False, "non-json-200"
        if body.get("code") in [401, 404] or body.get("success") is False:
            return True, "json-semantic"
    return False, "ok"


def update_data_cycle(cookie_override=None):
    """One fetch -> parse -> atomic write -> !Refresh cycle.

    Args:
        cookie_override: when provided (non-empty), this fresh cookie header
            is used directly instead of load_cookies(). Used by the manual
            --interactive-login one-shot so the just-captured session is
            fetched immediately without a redundant disk read.

    Returns True on success, False on fetch/expiry failure.

    Spec C (passive auth): on expiry with AUTO_RELOGIN=0 the widget shows
    Status=expired with the LAST KNOWN valid packages retained (never
    wiped); NO browser is launched. Spec B: this cycle runs regardless of
    pause state whenever the caller decides to fetch (manual trigger);
    the caller never clears `.paused`.
    """
    if cookie_override:
        cookie = cookie_override
    else:
        cookie = load_cookies()
    res = fetch_data(cookie)

    expired, reason = _is_session_expired(res)

    if expired:
        if AUTO_RELOGIN:  # legacy opt-in behavior (spec C default is OFF)
            log.warning(
                "Session expired (%s) - AUTO_RELOGIN=1, relaunching login...",
                reason)
            cookie = get_fresh_cookies_auto()
            res = fetch_data(cookie)
            expired, reason = _is_session_expired(res)
        if expired:
            log.error(
                "Session expired (%s) and no autonomous login - "
                "Status=expired; middle-click to re-auth.", reason)
            # Retain last known valid packages on screen (spec C: never
            # wipe the UI on expiry).
            _record_and_write(LAST_PACKAGES or [], "expired",
                              time.strftime("%I:%M:%S %p"))
            return False

    if res is not None and res.status_code == 200:
        # Bug #2: guard the post-(re)login JSON parse; both exception names
        # are listed explicitly (JSONDecodeError subclasses ValueError).
        try:
            res_json = res.json()
        except (ValueError, requests.exceptions.JSONDecodeError) as e:
            log.exception("HTTP 200 returned non-JSON body: %s", e)
            _record_and_write([], "error", time.strftime("%I:%M:%S %p"))
            return False

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

        # Save raw JSON for debug (atomic so readers never see torn JSON)
        updated_at = time.strftime("%I:%M:%S %p")
        _data_tmp = DATA_FILE + ".tmp"
        with open(_data_tmp, "w", encoding="utf-8") as f:
            json.dump({
                "status": "success",
                "updated_at": updated_at,
                "packages": output_packages
            }, f)
        os.replace(_data_tmp, DATA_FILE)

        # Save Rainmeter-friendly variables.inc with FULL cache update so a
        # later pause toggle can rewrite the feed without any network I/O.
        _record_and_write(output_packages, "success", updated_at)

        print(f"[+] Successfully extracted {len(output_packages)} active packages.")
        log.info("Fetch succeeded: %d active packages (paused=%s).",
                 len(output_packages), _PAUSED)
        return True
    else:
        status = res.status_code if res else "No Response"
        print(f"[!] Fetch failed with status: {status}")
        log.error("Fetch failed with status: %s", status)
        # Write error state so the widget shows a message instead of stale blanks.
        try:
            _record_and_write([], "error", time.strftime("%I:%M:%S %p"))
        except Exception as e:
            print(f"[!] Could not write error state: {e}")
            log.exception("Could not write error state")
        return False


def run_interactive_login_once():
    """On-demand manual sync for `--interactive-login` (forced re-login).

    1. Headed browser login (OTP visible) -> capture cookies/bearer tokens.
    2. Persist to cookies.json + .env.
    3. Immediate usage fetch -> clean metrics -> variables.inc -> !Refresh.
    4. Browser always closed cleanly; no child processes left behind.
    Returns True on full success, False otherwise.
    """
    print("[*] Starting on-demand interactive sync...")
    log.info("Starting on-demand interactive sync.")
    cookie = get_fresh_cookies_interactive()
    if not cookie:
        print("[!] Interactive login did not produce a session. "
              "Leaving current widget data untouched.")
        log.error("Interactive login produced no session.")
        return False
    print("[*] Session captured - fetching usage immediately...")
    # write_variables_inc() inside update_data_cycle triggers
    # `Rainmeter.exe !Refresh DialogMonitor` on success.
    ok = update_data_cycle(cookie_override=cookie)
    if ok:
        print("[+] Manual sync complete - widget refreshed.")
        log.info("Interactive sync complete - widget refreshed.")
    else:
        print("[!] Manual sync login succeeded but usage fetch failed.")
        log.error("Interactive sync: login ok but usage fetch failed.")
    return ok


def consume_refresh_trigger():
    """Consume a pending manual refresh request, if any.

    Returns True if a `.refresh.trigger` file existed (and was removed),
    False otherwise. Never raises. Retries once on Windows file-lock
    races (AV/indexer holding the trigger for a few ms).
    """
    try:
        if not os.path.exists(TRIGGER_FILE):
            return False
        try:
            os.remove(TRIGGER_FILE)
        except FileNotFoundError:
            pass
        except PermissionError:
            # Transient lock (just-written trigger) - brief retry once.
            try:
                time.sleep(0.05)
                os.remove(TRIGGER_FILE)
            except FileNotFoundError:
                pass
        return True
    except Exception as e:
        print(f"[!] Could not consume refresh trigger: {e}")
        log.error("Could not consume refresh trigger: %s", e)
    return False


def run_daemon_loop(initial_fetch_done=False):
    """Standard background daemon loop (single owner of the mutex).

    - Wakes every TRIGGER_POLL_SECONDS (1s) to consume `.refresh.trigger`
      requests from manual `--refresh` launches for instant UI feedback.
    - Checks the `.paused` flag in the SAME 1s iteration (spec A: zero
      extra wakeups): while paused, periodic fetches are skipped but
      manual triggers still fetch (spec B: one-shot, pause retained).
    - Does periodic usage fetches every POLL_INTERVAL (default 15 min).
    - Never returns under normal operation; Ctrl+C breaks cleanly.
    """
    log.info("Daemon starting - auto-relogin=%s, auto-poll=%ss",
             "ON" if AUTO_RELOGIN else "OFF (passive)", POLL_INTERVAL)
    consume_refresh_trigger()  # drop stale triggers so boot doesn't double-poll
    _refresh_pause_state()     # detect a `.paused` flag left over from last session
    if initial_fetch_done:
        # The caller just synced (instant UI feedback); schedule the next
        # periodic fetch POLL_INTERVAL from now.
        pass
    elif _PAUSED:
        # Spec A: paused = no autonomous network activity, even at boot.
        log.info("Daemon started while PAUSED - skipping initial fetch.")
    else:
        # Normal daemon startup: fetch immediately for instant UI.
        try:
            update_data_cycle()
        except Exception:
            log.exception("Initial daemon fetch failed")
    last_fetch = time.monotonic()
    print(f"[*] Daemon loop started - trigger poll "
          f"{TRIGGER_POLL_SECONDS:g}s, periodic fetch every "
          f"{POLL_INTERVAL}s.")
    while True:
        try:
            paused = _refresh_pause_state()
            if consume_refresh_trigger():
                if paused:
                    # Spec B: explicit manual refresh while paused MUST
                    # fetch and update the UI, but MUST NOT clear `.paused`.
                    log.info("Manual refresh while PAUSED - one-shot fetch, "
                             "pause retained.")
                print("[*] Manual refresh trigger consumed - fetching now.")
                try:
                    update_data_cycle()
                except Exception:
                    log.exception("Triggered fetch failed")
                last_fetch = time.monotonic()
                continue
            remaining = POLL_INTERVAL - (time.monotonic() - last_fetch)
            if remaining <= 0 and not paused:
                try:
                    update_data_cycle()
                except Exception:
                    log.exception("Periodic fetch failed")
                last_fetch = time.monotonic()
                continue
            if remaining <= 0:
                # Paused: hold the countdown at zero so resume doesn't
                # fire an immediate burst fetch after a long pause.
                last_fetch = time.monotonic()
                continue
            time.sleep(min(TRIGGER_POLL_SECONDS, remaining))
        except KeyboardInterrupt:
            print("[*] Daemon loop interrupted - exiting.")
            log.info("Daemon loop interrupted - exiting.")
            break
        except Exception:
            log.exception("Daemon loop error")
            time.sleep(TRIGGER_POLL_SECONDS)


def run_refresh_and_promote_to_daemon():
    """Handle `main.pyw --refresh` when NO daemon is running (fail-safe).

    1. Mutex is already held by this process (acquired in the guard).
    2. Do an initial sync/fetch now for instant UI feedback (skipped when
       paused - spec A: no autonomous network while paused).
    3. DO NOT EXIT - transition into run_daemon_loop() so the system is
       left with a running background daemon.
    """
    print("[*] No daemon detected - --refresh auto-promoting to daemon.")
    log.info("--refresh auto-promoting to daemon (no daemon was alive).")
    _refresh_pause_state()
    if _PAUSED:
        log.info("Promoting to daemon while PAUSED - no initial fetch.")
        run_daemon_loop(initial_fetch_done=False)
        return
    print("[*] Performing initial sync for instant UI feedback...")
    try:
        update_data_cycle()
        initial_done = True
    except Exception:
        log.exception("Initial refresh fetch failed - entering daemon loop to retry.")
        initial_done = False
    run_daemon_loop(initial_fetch_done=initial_done)


if __name__ == "__main__":
    if SHOW_HELP:
        print("Usage:")
        print(f"  main.pyw                  Run background daemon "
              f"({POLL_INTERVAL // 60}-min periodic fetch, 1s trigger poll).")
        print("  main.pyw --refresh        "
              "Lightweight manual sync: signal daemon if running "
              "(~0.1s exit), else fetch once and auto-promote to daemon.")
        print("  main.pyw --toggle-pause   "
              "Fast (~1ms): flip `.paused` and notify the daemon. Explicit "
              "--pause / --resume variants provided. Works daemoneless too.")
        print("  main.pyw --interactive-login  "
              "On-demand headed login + immediate sync + skin refresh.")
        sys.exit(0)
    if (TOGGLE_PAUSE_REQUESTED or PAUSE_REQUESTED or RESUME_REQUESTED):
        # No-daemon path lands here (the daemon-alive case exited pre-import
        # inside _ensure_single_instance). Pure flag flip + exit.
        _handle_pause_request()
        sys.exit(0)
    try:
        # Spec E: mandatory .env identity - clean config_error exit BEFORE
        # any network or browser attempt (applies to every mode below).
        _validate_identity()
        if REFRESH_REQUESTED and not INTERACTIVE_LOGIN:
            if ANOTHER_INSTANCE_RUNNING:
                # Late safety net (the early pre-import fast path in
                # _ensure_single_instance() normally handles this): signal
                # the live daemon and exit immediately.
                print("[*] Daemon already running - refresh requested.")
                _signal_daemon_refresh()
                sys.exit(0)
            # Fail-safe: no daemon - fetch once, then stay as the daemon.
            run_refresh_and_promote_to_daemon()
        elif INTERACTIVE_LOGIN:
            ok = run_interactive_login_once()
            if ANOTHER_INSTANCE_RUNNING:
                # Daemon owns the loop - exit one-shot with status code.
                sys.exit(0 if ok else 1)
            # No daemon was running: stay alive as the daemon so the widget
            # keeps refreshing (1s trigger poll + periodic fetches).
            print("[*] No daemon was running - continuing as background daemon.")
            if not ok:
                # Still enter loop; next cycle retries automatically.
                pass
            run_daemon_loop(initial_fetch_done=ok)
        else:
            run_daemon_loop(initial_fetch_done=False)
    except SystemExit:
        raise
    except Exception:
        # Unhandled-exception observability: full stack trace -> daemon.log
        # (spec D), then the original exception propagates.
        log.exception("Unhandled fatal error - exiting")
        raise
