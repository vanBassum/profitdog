"""The three pages that exist outside the React app.

Signing in, approving a PC, and fetching the agent are all things that have to
work *before* there is a session, or before the bundle is worth loading. They
are server-rendered for that reason, and kept deliberately plain: no build
step, no bundle, no dependency on the UI having been built at all. A server
with no `dist/` still lets someone sign in and link a machine.

The styling is inline and minimal on purpose. These pages are seen for a few
seconds each, and the alternative -- a second stylesheet to keep in step with
the real UI -- is a maintenance cost with nothing behind it.
"""

from __future__ import annotations

import html

_STYLE = """
  :root { color-scheme: light dark; --fg:#111; --bg:#fafafa; --card:#fff;
          --muted:#666; --line:#e4e4e7; --accent:#2563eb; }
  @media (prefers-color-scheme: dark) {
    :root { --fg:#f4f4f5; --bg:#09090b; --card:#18181b; --muted:#a1a1aa;
            --line:#27272a; --accent:#60a5fa; }
  }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         background:var(--bg); color:var(--fg); padding:24px;
         font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif; }
  .card { width:100%; max-width:420px; background:var(--card);
          border:1px solid var(--line); border-radius:14px; padding:28px; }
  h1 { margin:0 0 6px; font-size:19px; letter-spacing:-0.01em; }
  p  { margin:0 0 16px; color:var(--muted); }
  .brand { font-weight:600; letter-spacing:-0.02em; margin-bottom:18px; }
  .btn { display:block; width:100%; padding:11px 16px; border-radius:9px;
         border:1px solid var(--line); background:var(--accent); color:#fff;
         font:inherit; font-weight:550; text-align:center; text-decoration:none;
         cursor:pointer; }
  .btn.secondary { background:transparent; color:var(--fg); margin-top:9px; }
  .code { font:600 26px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;
          letter-spacing:.14em; text-align:center; padding:16px;
          border:1px dashed var(--line); border-radius:10px; margin-bottom:16px; }
  .row { display:flex; gap:9px; align-items:center; margin-bottom:9px; }
  .muted { color:var(--muted); font-size:13px; }
  .err { color:#dc2626; }
  input[type=text] { width:100%; padding:11px 13px; border-radius:9px;
                     border:1px solid var(--line); background:transparent;
                     color:var(--fg); font:inherit; margin-bottom:12px; }
  ol { margin:0 0 16px; padding-left:20px; color:var(--muted); }
  li { margin-bottom:7px; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>" + html.escape(title) + " &middot; profitdog</title>"
        "<style>" + _STYLE + "</style></head><body><main class=\"card\">"
        "<div class=\"brand\">profitdog</div>" + body + "</main></body></html>"
    )


def login_page(*, next_url: str = "/", error: str | None = None,
               configured: bool = True) -> str:
    if not configured:
        return _page(
            "Sign in",
            "<h1>Sign-in is not configured</h1>"
            "<p>This server has no Google client set. Set "
            "<code>PROFITDOG_GOOGLE_CLIENT_ID</code> and "
            "<code>PROFITDOG_GOOGLE_CLIENT_SECRET</code> and restart it.</p>",
        )
    note = (
        "<p class=\"err\">" + html.escape(error) + "</p>" if error else
        "<p>Your play history is private to your account.</p>"
    )
    target = html.escape(next_url or "/", quote=True)
    return _page(
        "Sign in",
        "<h1>Sign in</h1>" + note +
        "<a class=\"btn\" href=\"/auth/login?next=" + target + "\">"
        "Continue with Google</a>",
    )


def link_page(*, code: str, agent_label: str | None, error: str | None = None,
              done: bool = False) -> str:
    if done:
        return _page(
            "PC linked",
            "<h1>This PC is linked</h1>"
            "<p>You can close this tab. The agent will finish on its own and "
            "start sending matches to your account.</p>"
            "<a class=\"btn\" href=\"/\">Open profitdog</a>",
        )
    if error:
        return _page(
            "Link a PC",
            "<h1>That code did not work</h1>"
            "<p class=\"err\">" + html.escape(error) + "</p>"
            "<p>Codes last ten minutes and work once. Start the agent again "
            "to get a new one.</p>"
            "<a class=\"btn secondary\" href=\"/\">Open profitdog</a>",
        )
    who = (
        "<p>Approving will let <strong>" + html.escape(agent_label) +
        "</strong> upload matches to your account.</p>"
        if agent_label else
        "<p>Approving will let this PC upload matches to your account.</p>"
    )
    return _page(
        "Link a PC",
        "<h1>Link this PC?</h1>"
        "<div class=\"code\">" + html.escape(code) + "</div>" + who +
        "<p class=\"muted\">Only approve a code you are looking at on your own "
        "screen. It grants upload access only &mdash; a linked PC can never "
        "read your history.</p>"
        "<form method=\"post\" action=\"/link/approve\">"
        "<input type=\"hidden\" name=\"code\" value=\"" +
        html.escape(code, quote=True) + "\">"
        "<button class=\"btn\" type=\"submit\">Approve this PC</button>"
        "</form>"
        "<a class=\"btn secondary\" href=\"/\">Cancel</a>",
    )


def link_prompt_page(*, error: str | None = None) -> str:
    """Asks for the code, when someone opened /link without one."""
    note = "<p class=\"err\">" + html.escape(error) + "</p>" if error else (
        "<p>Enter the code the agent is showing on the PC you want to link.</p>"
    )
    return _page(
        "Link a PC",
        "<h1>Link a PC</h1>" + note +
        "<form method=\"get\" action=\"/link\">"
        "<input type=\"text\" name=\"code\" placeholder=\"ABCD-1234\" "
        "autocomplete=\"off\" autocapitalize=\"characters\" autofocus>"
        "<button class=\"btn\" type=\"submit\">Continue</button>"
        "</form>",
    )


def download_page(*, available: bool, releases_url: str) -> str:
    """The setup page: where the agent comes from, and what to do with it.

    Two ways to get the same program. A server that has a build beside it
    serves those bytes; a hosted one has none -- the agent is frozen on a
    Windows runner and published on GitHub -- and sends people to the
    releases instead. Both paths are shown whenever both work, because a
    download that is one hop from the source is easier to trust than one
    that appears out of a server you happen to be signed in to.
    """
    releases = html.escape(releases_url or "", quote=True)
    steps = (
        "<ol>"
        "<li>Download the agent and run it on the PC you play on.</li>"
        "<li>It shows a code and opens this site in your browser.</li>"
        "<li>Sign in, approve the code, and it starts sending matches here.</li>"
        "</ol>"
    )
    have_code = "<a class=\"btn secondary\" href=\"/link\">I already have a code</a>"

    if not available:
        # Not an error. This is the normal shape of a hosted instance: the
        # image has no EXE in it, and the release is where the EXE is.
        body = (
            "<h1>Set up a gaming PC</h1>"
            "<p>profitdog only sees your matches once the agent is running on "
            "the PC you play on. Nothing is tracked until then.</p>" + steps
        )
        if releases:
            body += (
                "<a class=\"btn\" href=\"" + releases + "\" target=\"_blank\" "
                "rel=\"noreferrer noopener\">Get profitdog.exe from Releases</a>"
                + have_code +
                "<p class=\"muted\" style=\"margin-top:16px\">Grab "
                "<code>profitdog.exe</code> from the latest release. The same "
                "build works for everyone; it belongs to your account only "
                "once you approve it.</p>"
            )
        else:
            body += (
                "<p class=\"err\">This server has no agent build and no "
                "releases URL. Build one with <code>python -m PyInstaller "
                "agent.spec</code>, or point "
                "<code>PROFITDOG_AGENT_EXE</code> or "
                "<code>PROFITDOG_RELEASES_URL</code> at one.</p>"
                + have_code
            )
        return _page("Download the agent", body)

    release_link = (
        "<p class=\"muted\" style=\"margin-top:16px\">Or take it from "
        "<a href=\"" + releases + "\" target=\"_blank\" "
        "rel=\"noreferrer noopener\">GitHub Releases</a>, where every build "
        "is published.</p>"
        if releases else ""
    )
    return _page(
        "Download the agent",
        "<h1>Set up a gaming PC</h1>"
        "<p>profitdog only sees your matches once the agent is running on the "
        "PC you play on. Nothing is tracked until then.</p>" + steps +
        "<a class=\"btn\" href=\"/download/profitdog.exe\">Download for Windows</a>"
        + have_code +
        "<p class=\"muted\" style=\"margin-top:16px\">The same build works for "
        "everyone; it belongs to your account only once you approve it.</p>"
        + release_link,
    )
