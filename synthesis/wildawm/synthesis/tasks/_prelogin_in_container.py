#!/usr/bin/env python3
"""Run inside the agent container: log the persona into each web mirror and save a storageState."""
import http.cookiejar
import json
import re
import sys
import urllib.parse
import urllib.request

from wildawm import const_config as cfg

PASSWORD = "wildawm"


def login(domain: str, email: str, login_path: str = "/login") -> list[dict]:
    base = f"https://{domain}"
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPRedirectHandler())
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        page = op.open(
            base + login_path,
            timeout=cfg.HTTP_SHORT_TIMEOUT_S).read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  [{domain}] GET {login_path} failed: {e}", file=sys.stderr)
        return []
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
    fields = {"email": email, "password": PASSWORD}
    if m:
        fields["csrf_token"] = m.group(1)
    data = urllib.parse.urlencode(fields).encode()
    try:
        op.open(base + login_path, data=data,
                timeout=cfg.HTTP_SHORT_TIMEOUT_S)
    except Exception as e:
        print(f"  [{domain}] POST {login_path} failed: {e}", file=sys.stderr)
    cookies = []
    for c in cj:
        cookies.append({
            "name": c.name, "value": c.value,
            "domain": c.domain if c.domain.startswith(".") else c.domain,
            "path": c.path or "/", "expires": -1,
            "httpOnly": bool(c.get_nonstandard_attr("HttpOnly", False)) or c.name == "session",
            "secure": bool(c.secure), "sameSite": "Lax"})
    return cookies


def main():
    accounts = json.loads(sys.argv[1])
    out_path = sys.argv[2]
    all_cookies = []
    ok = []
    for a in accounts:
        cs = login(a["domain"], a["email"], a.get("login_path", "/login"))
        if cs:
            all_cookies.extend(cs)
            ok.append(a["domain"])
    state = {"cookies": all_cookies, "origins": []}
    with open(out_path, "w") as f:
        json.dump(state, f)
    print(json.dumps({"logged_in": ok, "n_cookies": len(all_cookies)}))


if __name__ == "__main__":
    main()
