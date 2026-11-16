"""One-time sign-in helper for the tonearm Chrome profile.

Default: opens a HEADED Chrome window so you can sign in manually.
Interactive/headless (SSH-friendly): drives the Apple sign-in flow from the
terminal — prompts for email/password/verification codes and fills them in
Chrome via CDP.

  python -m tonearm.login                  # headed/manual (default with DISPLAY)
  python -m tonearm.login --interactive    # terminal-driven, works over SSH
  python -m tonearm.login --headless       # alias for --interactive
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import websocket

from .config import config

MUSIC_URL = "https://music.apple.com/"
JS_READY = """(() => {
  try {
    const inst = window.MusicKit && MusicKit.getInstance();
    if (!inst) return {ready: false, authorized: false};
    return {ready: !!inst.musicUserToken, authorized: !!inst.musicUserToken};
  } catch (e) { return {ready: false, authorized: false}; }
})()"""


class _CDP:
    """Minimal CDP client for the login flow (independent of Browser singleton)."""

    def __init__(self, ws_url: str, timeout: int = 30):
        self.ws = websocket.create_connection(ws_url, timeout=timeout)
        self.n = 0

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def call(self, method: str, **params):
        self.n += 1
        msg_id = self.n
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expression: str, timeout: int = 30):
        res = self.call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
        if "exceptionDetails" in res:
            raise RuntimeError(str(res["exceptionDetails"])[:300])
        return (res.get("result") or {}).get("value")


def _cdp_http(path: str, timeout: int = 3):
    return json.loads(urllib.request.urlopen(config.cdp_url + path, timeout=timeout).read())


def _wait_cdp(timeout: int = 30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            _cdp_http("/json/version", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _ensure_xvfb(display: str) -> subprocess.Popen | None:
    """Start Xvfb for a display if nothing is serving it (best-effort)."""
    if not display.startswith(":"):
        return None
    if shutil.which("Xvfb") is None:
        return None
    # quick probe: try xdpyinfo/xset not required; just attempt launch and let
    # Chrome fail later if unusable. Avoid clobbering an existing display by
    # checking the socket.
    sock = f"/tmp/.X11-unix/X{display[1:]}"
    if os.path.exists(sock):
        return None
    try:
        return subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1280x900x24", "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return None


def _launch_chrome(headless: bool):
    port = config.cdp_url.rsplit(":", 1)[-1]
    cmd = [
        config.chrome_bin,
        f"--user-data-dir={config.profile_dir}",
        f"--remote-debugging-port={port}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1280,900",
    ]
    env = os.environ.copy()
    xvfb_proc = None
    if headless:
        # Prefer headless=new for SSH; Apple sign-in sometimes needs a display,
        # so if no DISPLAY is set we try to provide Xvfb and run headed-chrome
        # on it as a fallback path that still works without a real monitor.
        if not env.get("DISPLAY") and shutil.which("Xvfb"):
            display = env.get("TONEARM_LOGIN_DISPLAY", ":98")
            xvfb_proc = _ensure_xvfb(display)
            env["DISPLAY"] = display
        if env.get("DISPLAY"):
            # headed on virtual display is often more compatible than headless
            cmd += ["--no-sandbox", "--disable-gpu"]
        else:
            cmd += ["--headless=new", "--no-sandbox", "--disable-gpu"]
    cmd.append(MUSIC_URL)
    print(f"Launching Chrome ({'interactive/headless' if headless else 'headed'})…")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=env,
    )
    return proc, xvfb_proc


def _get_music_tab_ws():
    tabs = _cdp_http("/json/list", timeout=5)
    target = next((t for t in tabs if t.get("type") == "page" and "music.apple.com" in t.get("url", "")), None)
    if target is None:
        # navigate an existing page or create context by asking chrome to open
        target = next((t for t in tabs if t.get("type") == "page"), None)
    if not target:
        raise RuntimeError("no Chrome page target found")
    return target.get("webSocketDebuggerUrl"), target.get("id")


def _js_click_sign_in():
    # try common selectors/text for the Music web sign-in button
    return r"""(() => {
      const byText = (root, texts) => {
        const els = Array.from(root.querySelectorAll('button, a, [role="button"]'));
        return els.find(e => texts.some(t => (e.innerText||e.getAttribute('aria-label')||'').toLower().includes(t)));
      };
      const b = byText(document, ['sign in','sign-in','log in']);
      if (b) { b.click(); return {clicked:true, where:'button'}; }
      // try within shadow/open nav
      return {clicked:false};
    })()"""


def _js_fill_and_continue(email: str):
    return """(() => {
      const email = %s;
      const doc = document;
      const frames = [doc, ...Array.from(doc.querySelectorAll('iframe')).map(f=>{try{return f.contentDocument}catch(e){return null}}).filter(Boolean)];
      const find = (sels) => {
        for (const d of frames) for (const s of sels) { const el = d.querySelector(s); if (el) return {el,d}; }
        return null;
      };
      const setVal = (el, v) => {
        const proto = el.tagName==='TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && desc.set) desc.set.call(el, v); else el.value = v;
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
      };
      let r = find(['input#account_name_text_field','input[name="accountName"]','input[type="email"]','input[autocomplete="username"]']);
      if (r) { setVal(r.el, email); r.el.focus(); }
      // click continue/next arrow
      const click = (d) => {
        const sels = ['button#sign-in','button#continue','button[type="submit"]','button.button-primary','idms-step button'];
        for (const s of sels) { const b = d.querySelector(s); if (b) { b.click(); return s; } }
        const by = Array.from(d.querySelectorAll('button,[role="button"]')).find(e=>/continue|next|sign in|^$/i.test((e.innerText||'').trim()) || (e.getAttribute('aria-label')||'').toLower().includes('continue'));
        if (by) { by.click(); return 'text'; }
        return null;
      };
      let clicked = null;
      for (const d of frames) { clicked = click(d); if (clicked) break; }
      return {filled: !!r, clicked: clicked};
    })()""" % (json.dumps(email))


def _js_fill_password(pw: str):
    return """(() => {
      const pw = %s;
      const frames = [document, ...Array.from(document.querySelectorAll('iframe')).map(f=>{try{return f.contentDocument}catch(e){return null}}).filter(Boolean)];
      const setVal = (el, v) => {
        const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
        if (desc && desc.set) desc.set.call(el, v); else el.value = v;
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
      };
      for (const d of frames) {
        const el = d.querySelector('input[type="password"], input#password_text_field, input[name="password"]');
        if (el) {
          setVal(el, pw); el.focus();
          const b = d.querySelector('button#sign-in, button[type="submit"], button.button-primary') ||
                    Array.from(d.querySelectorAll('button,[role="button"]')).find(e=>/sign in|continue|next/i.test((e.innerText||e.getAttribute('aria-label')||'')));
          if (b) b.click();
          return {filled:true, clicked: !!b};
        }
      }
      return {filled:false, clicked:false};
    })()""" % (json.dumps(pw))


def _js_submit_code(code: str):
    # Apple sometimes uses 6 separate digit inputs
    return r"""(() => {
      const code = %s;
      const frames = [document, ...Array.from(document.querySelectorAll('iframe')).map(f=>{try{return f.contentDocument}catch(e){return null}}).filter(Boolean)];
      const setVal = (el, v) => {
        const proto = el.tagName==='TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && desc.set) desc.set.call(el, v); else el.value = v;
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
        el.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true}));
      };
      for (const d of frames) {
        const digits = Array.from(d.querySelectorAll('input.form-security-code-input, input[data-index], input[autocomplete="one-time-code"]'));
        if (digits.length >= 4) {
          const chars = code.replace(/\s+/g,'').split('');
          digits.forEach((el,i)=>{ if(chars[i]) setVal(el, chars[i]); });
          return {filled:'digits', count: digits.length};
        }
        const single = d.querySelector('input#char0, input[name="verificationCode"], input[type="tel"], input[inputmode="numeric"]');
        if (single) { setVal(single, code); single.dispatchEvent(new Event('input',{bubbles:true})); return {filled:'single'}; }
      }
      return {filled:false};
    })()""" % (json.dumps(code))


def _js_page_state():
    return r"""(() => {
      try {
        const inst = window.MusicKit && MusicKit.getInstance();
        const authorized = !!(inst && inst.musicUserToken);
        const txt = (document.body && document.body.innerText || '').toLower();
        const needsCode = /verification code|two-factor|two factor|enter the code|trusted device|code sent/.test(txt);
        const needsPassword = !!document.querySelector('input[type="password"]') ||
          Array.from(document.querySelectorAll('iframe')).some(f=>{try{return !!f.contentDocument.querySelector('input[type="password"]')}catch(e){return false}});
        return {authorized, needsCode, needsPassword, url: location.href};
      } catch(e) { return {authorized:false, error:String(e), url: location.href}; }
    })()"""


def interactive_login(timeout: int = 300) -> int:
    """Terminal-driven sign-in flow over CDP (SSH/headless friendly)."""
    if not sys.stdin.isatty():
        print("interactive login requires a TTY (run from an interactive SSH/session)", file=sys.stderr)
        return 2

    # best-effort: reuse existing debuggable Chrome if present, else launch
    have_cdp = _wait_cdp(timeout=2)
    chrome_proc = xvfb_proc = None
    if not have_cdp:
        chrome_proc, xvfb_proc = _launch_chrome(headless=True)
        if not _wait_cdp(timeout=40):
            print("Chrome debug port did not come up. Install Chrome/Chromium and retry.", file=sys.stderr)
            return 1
    else:
        print(f"Attaching to existing Chrome at {config.cdp_url}…")

    try:
        ws_url, _ = _get_music_tab_ws()
        cdp = _CDP(ws_url)
    except Exception as e:
        print(f"Could not attach to a Chrome tab: {e}", file=sys.stderr)
        return 1

    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Page.navigate", url=MUSIC_URL)
        time.sleep(4)

        # quick check: maybe already signed in
        try:
            st = cdp.eval(JS_READY)
            if st and st.get("authorized"):
                print("Already signed in on this profile — nothing to do.")
                return 0
        except Exception:
            pass

        print("\nHeadless/SSH sign-in: your credentials are typed here and sent only")
        print("into the local Chrome session on this machine (never logged).\n")

        # trigger sign-in UI
        try:
            cdp.eval(_js_click_sign_in())
        except Exception:
            pass
        time.sleep(2)

        email = input("Apple ID (email): ").strip()
        if not email:
            print("No email provided, aborting.", file=sys.stderr)
            return 2
        try:
            cdp.eval(_js_fill_and_continue(email))
        except Exception as e:
            print(f"Note: could not auto-fill email ({e}). You may need headed login.")
        time.sleep(3)

        # wait/prompt for password step; Apple may take a moment to show it
        t0 = time.time()
        pw_sent = False
        code_prompted = False
        while time.time() - t0 < timeout:
            try:
                st = cdp.eval(_js_page_state(), timeout=10) or {}
            except Exception:
                st = {}
            if st.get("authorized"):
                print("\nSigned in successfully. Session saved to profile:")
                print(f"  {config.profile_dir}")
                return 0

            if st.get("needsCode") and not code_prompted:
                code = input("Verification code (from your trusted device/SMS): ").strip()
                if code:
                    try:
                        cdp.eval(_js_submit_code(code))
                    except Exception as e:
                        print(f"Could not submit code automatically: {e}")
                code_prompted = True
                time.sleep(3)
                continue
            # reset code prompt if state flips back later
            if not st.get("needsCode"):
                code_prompted = False

            if st.get("needsPassword") and not pw_sent:
                pw = getpass.getpass("Apple ID password: ")
                try:
                    cdp.eval(_js_fill_password(pw))
                    pw_sent = True
                except Exception as e:
                    print(f"Could not fill password automatically: {e}")
                time.sleep(3)
                continue

            # if we haven't seen password field yet, keep nudging continue
            if not pw_sent:
                try:
                    cdp.eval(_js_fill_and_continue(email))
                except Exception:
                    pass
            time.sleep(2)

        print("Timed out waiting for authorization. If Apple's flow differed, run:", file=sys.stderr)
        print("  python -m tonearm.login   (headed/manual) or retry --interactive", file=sys.stderr)
        return 1
    finally:
        cdp.close()
        # leave chrome/profile intact; kill the one we launched if desired
        if chrome_proc is not None:
            print("Stopping the login Chrome instance…")
            try:
                chrome_proc.terminate()
            except Exception:
                pass
        if xvfb_proc is not None:
            try:
                xvfb_proc.terminate()
            except Exception:
                pass


def headed_login():
    print("Opening a visible Chrome window on the tonearm profile…")
    print("Sign in to https://music.apple.com, then come back here.")
    proc = subprocess.Popen([
        config.chrome_bin,
        f"--user-data-dir={config.profile_dir}",
        "--remote-debugging-port=" + config.cdp_url.rsplit(":", 1)[-1],
        "--no-first-run", "--no-default-browser-check",
        MUSIC_URL,
    ])
    try:
        input("\nPress Enter here AFTER you've signed in and can see your library… ")
    finally:
        print("Leaving that window open — tonearm can attach to it, or close it and run headless.")
        print("(If you close it, just start the server: it relaunches headless with your session.)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m tonearm.login", add_help=True)
    p.add_argument("--interactive", "--headless", dest="interactive", action="store_true",
                   help="terminal-driven sign-in over CDP (SSH/headless friendly)")
    p.add_argument("--headed", action="store_true", help="force manual headed browser sign-in")
    p.add_argument("--timeout", type=int, default=300, help="interactive auth timeout in seconds")
    args = p.parse_args(argv)

    interactive = args.interactive
    if args.headed:
        interactive = False
    elif not interactive:
        # auto: no display in an interactive terminal -> use interactive flow
        if not os.environ.get("DISPLAY") and sys.stdin.isatty():
            interactive = True

    if interactive:
        rc = interactive_login(timeout=args.timeout)
        raise SystemExit(rc)
    headed_login()


if __name__ == "__main__":
    main()
