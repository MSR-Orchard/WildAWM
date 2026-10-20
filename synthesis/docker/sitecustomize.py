"""Container-only patches for side effects of the frozen world clock."""

# The frozen clock makes real certificates look not-yet-valid, so skip verification.
import os
import re
import ssl

_orig_create_default_context = ssl.create_default_context

def _no_verify_context(*args, **kwargs):
    ctx = _orig_create_default_context(*args, **kwargs)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

ssl.create_default_context = _no_verify_context
ssl._create_default_https_context = _no_verify_context

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass


# Prepend the frozen date so it overrides any provider-injected current date.
def _read_frozen_clock():
    """Parse /etc/faketimerc; return ('YYYY-MM-DD', 'YYYY-MM-DD HH:MM') or None."""
    if not os.path.exists("/etc/faketimerc"):
        return None
    try:
        with open("/etc/faketimerc") as f:
            content = f.read().strip()
    except OSError:
        return None
    m = re.match(r"^@(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2})(?::\d{2})?)?", content)
    if not m:
        return None
    date = m.group(1)
    time_ = m.group(2) or "23:59"
    return date, f"{date} {time_}"


def _patch_nanobot_system_prompt():
    pair = _read_frozen_clock()
    if not pair:
        return
    date, datetime_str = pair
    try:
        from nanobot.agent.context import ContextBuilder
    except Exception:
        return
    if getattr(ContextBuilder.build_system_prompt, "_wildawm_patched", False):
        return

    header = (
        "# WORLD CONTEXT — AUTHORITATIVE\n"
        f"You are operating inside a simulated world frozen at {datetime_str}.\n"
        f"**The current date is {date}.** This date is the only authoritative\n"
        "answer to any 'what day is it / what is today / current date / now'\n"
        "question. Do **not** use any other date you may have seen elsewhere\n"
        "(e.g. an injected hint from the model provider). When the user asks\n"
        "about time or recent events, anchor your answer to this date.\n"
    )
    _orig = ContextBuilder.build_system_prompt

    def _patched(self, *args, **kwargs):
        body = _orig(self, *args, **kwargs)
        return header + "\n---\n\n" + body

    _patched._wildawm_patched = True  # type: ignore[attr-defined]
    ContextBuilder.build_system_prompt = _patched


_patch_nanobot_system_prompt()
