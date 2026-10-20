"""Generate the per-instance /workspace/WORLD.md briefing read by every agent harness."""
import re
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.const_config import BROWSER_MCP_PORT, MCP_GATEWAY_URL
from wildawm.core.paths import PROJECT_ROOT, PROFILES_DIR
from wildawm.json_utils import load_jsonc

SITES_JSON = PROJECT_ROOT / "docker" / "webharbor" / "sites.json"

WEBHARBOR_SITE_DESCRIPTIONS = {
    "allrecipes": "Recipe search, collections, meal planning",
    "amazon": "Online shopping — product search, catalog pages, product images",
    "apple": "Apple Store — product browsing and product detail pages",
    "arxiv": "Academic paper archive — search, browse, save papers",
    "bbc_news": "BBC News — articles, categories, headlines",
    "booking": "Hotel & travel browsing — homepage, city pages, category pages, articles",
    "github": "Code hosting — repos, issues, PRs, profiles",
    "google_flights": "Flight search and comparison",
    "google_map": "Maps, places, directions, saved locations",
    "google_search": "Web search — search results, knowledge panel, featured snippets",
    "huggingface": "ML model hub — models, datasets, spaces",
    "wolfram_alpha": "Computational knowledge engine",
    "cambridge_dictionary": "English dictionary and thesaurus",
    "coursera": "Online courses — browse, enroll, study, quizzes",
    "espn": "Sports news, scores, standings, teams",
}

def _profile_path(profile_name: str) -> Path:
    for path in (
        cfg.WS_PROFILES_DIR / profile_name / "persona.md",
        PROFILES_DIR / profile_name / "persona.md",
        PROFILES_DIR / f"{profile_name}.md",
    ):
        if path.is_file():
            return path
    return cfg.WS_PROFILES_DIR / profile_name / "persona.md"


def _parse_profile(profile_name: str) -> dict:
    path = _profile_path(profile_name)
    if not path.exists():
        return {}
    text = path.read_text()
    info: dict = {"name": profile_name}

    for line in text.splitlines():
        line_s = line.strip()
        if line_s.startswith("# "):
            title = line_s[2:].strip()
            info["display_name"] = re.split(
                r"\s+(?:—|–|-)\s+|:\s*", title, maxsplit=1)[-1].strip()
        for key in ("Name", "Gender", "Age", "Occupation", "Location"):
            if line_s.startswith(f"- **{key}:**"):
                value = line_s.split(":**", 1)[1].strip()
                info[key.lower()] = value
                if key == "Name":
                    info["display_name"] = value

    m = re.search(r"## (?:Summary|Overview)\s*\n(.+?)(?=\n## |\Z)", text, re.DOTALL)
    if m:
        info["summary"] = m.group(1).strip()

    m = re.search(r"## (?:Relationships|Relationship network)\s*\n(.+?)(?=\n## |\Z)", text, re.DOTALL | re.IGNORECASE)
    if m:
        rels = []
        for line in m.group(1).strip().splitlines():
            rm = re.match(r"- \*\*(.+?)\*\*\s*[—–-]\s*(.+)", line)
            if rm:
                rels.append({"name": rm.group(1), "role": rm.group(2).strip()})
        info["relationships"] = rels

    m = re.search(r"## Web accounts.*?\n(.+?)(?=\n## |\Z)", text, re.DOTALL)
    if m:
        accounts = []
        for line in m.group(1).strip().splitlines():
            am = re.match(r"- \*\*(\w+)\*\*\s*[—–-]\s*(.+)", line)
            if am:
                accounts.append({"site": am.group(1), "detail": am.group(2).strip()})
        info["web_accounts"] = accounts

    return info


def _detect_services(data_dir: Path) -> list[str]:
    apps_dir = data_dir.parent / "overlay" / "apps"
    generated = {path.name for path in apps_dir.iterdir() if path.is_dir()} if apps_dir.is_dir() else set()
    return sorted(path.stem for path in data_dir.glob("*.db") if path.stem in generated) if data_dir.is_dir() else []


def _service_description(data_dir: Path, app_id: str) -> str:
    readme = data_dir.parent / "overlay" / "apps" / app_id / "README.md"
    if readme.is_file():
        paragraphs = [part.strip() for part in readme.read_text(errors="replace").split("\n\n")]
        for paragraph in paragraphs:
            if paragraph and not paragraph.startswith("#"):
                return " ".join(paragraph.split())
    return f"Generated MCP service `{app_id}` for this world."


def _workspace_tree(workspace_dir: Path, max_depth: int = 2) -> str:
    if not workspace_dir.is_dir():
        return "(empty)"
    lines = []
    for p in sorted(workspace_dir.iterdir()):
        if p.name.startswith(".") and p.name not in (".gitconfig", ".bashrc"):
            continue
        if p.is_dir():
            children = sorted(c.name for c in p.iterdir() if not c.name.startswith("."))[:8]
            suffix = f" ({', '.join(children)})" if children else ""
            if len(children) > 6:
                suffix = f" ({', '.join(children[:6])}, ...)"
            lines.append(f"  {p.name}/{suffix}")
        else:
            lines.append(f"  {p.name}")
    return "\n".join(lines) if lines else "(empty)"


def _load_webharbor_sites() -> list[dict]:
    if not SITES_JSON.exists():
        return []
    cfg = load_jsonc(SITES_JSON)
    sites = []
    for s in cfg.get("sites", []):
        sites.append({
            "id": s["id"],
            "domain": s["domains"][0],
            "description": WEBHARBOR_SITE_DESCRIPTIONS.get(s["id"], ""),
        })
    return sites


def render_world_md(
    profile_name: str,
    meta: dict,
    data_dir: Path,
    workspace_dir: Path,
) -> str:
    profile = _parse_profile(profile_name)
    display = profile.get("display_name", profile_name)
    active_svcs = _detect_services(data_dir)
    tree = _workspace_tree(workspace_dir)
    wh_sites = _load_webharbor_sites()

    frozen_date = meta.get("frozen_clock_date", "(unknown)")
    frozen_time = meta.get("frozen_clock_time", "10:00")
    day = meta.get("started_at_day", "?")

    sections = []

    sections.append(f"""# World Briefing

You are the personal AI assistant for **{display}**.

{profile.get('summary', '')}

| Field | Value |
|-------|-------|
| Name | {display} |
| Age | {profile.get('age', '—')} |
| Occupation | {profile.get('occupation', '—')} |
| Location | {profile.get('location', '—')} |
""")

    sections.append("""## Operating Mode — Autonomous

You run fully autonomously. **No human is available to answer questions
mid-task.** Any clarifying-question / ask-the-user / interactive prompt will
block forever and fail the task. Therefore:

- **NEVER ask the user a clarifying question and wait for a reply.** If a
  detail is ambiguous (dates, occupancy, which file, etc.), make the most
  reasonable assumption, state it briefly in your final answer, and proceed.
- Complete the entire task end-to-end in one go using the tools available.
- Only stop when the task is fully done (all requested files written, all
  emails/messages sent) — then give a short summary of what you did.
""")

    sections.append(f"""## Current Date & Time

Today is **{frozen_date}**, and the time is **{frozen_time}**.
This is the morning of Day {int(day) + 1 if str(day).isdigit() else day} in {display}'s world.

All clocks in this environment are frozen to this date. `date`, `timedatectl`,
and programmatic timestamp calls all return this date.
""")

    rels = profile.get("relationships", [])
    if rels:
        lines = [f"- **{r['name']}** — {r['role']}" for r in rels]
        sections.append("## Key People\n\n" + "\n".join(lines) + "\n")

    sections.append(f"""## Workspace

Your home directory is `/workspace` — this is {display}'s personal computer.

```
{tree}
```

These are {display}'s real files. Read them before modifying.
""")

    mcp_lines = []
    for svc in active_svcs:
        desc = _service_description(data_dir, svc)
        mcp_lines.append(f"| `{svc}` | {desc} |")
    sections.append(f"""## Structured Data Services (MCP)

Only the services in this table exist in the current world. They are already connected as MCP tools. List a service's available tools to discover its exact operations. Each service is backed by a real SQLite database populated with {display}'s history.

| Service | Description |
|---------|-------------|
{chr(10).join(mcp_lines)}

**IMPORTANT — never curl the gateway.** These services live behind an MCP
Streamable-HTTP gateway (`{MCP_GATEWAY_URL.rstrip()}/...`) that requires a protocol
handshake (initialize + session id); plain `curl`/`wget` to it fails with
`Not Acceptable` / `Missing session ID`. (`curl` is for the WebHarbor
**websites** below, never for these MCP services.)

Two ways to reach a service, in order of preference:
1. **Native MCP tools** — if your harness already exposes them as callable
   tools (e.g. `email_send`, `email_search`), just call them.
2. **Shell helper** — if you don't see those tools, use the pre-installed
   helper (handles the handshake for you):
   - List a service's tools: `python3 /agent-config/mcp_call.py email`
   - Call a tool: `python3 /agent-config/mcp_call.py email email_send '{{"to_recipients":"a@b.com","subject":"Hi","body":"..."}}'`
   (args are JSON; check the listed tool's schema for exact field names —
   e.g. recipients are comma-separated **strings**, not arrays.)

Services absent from the table are not implemented in this world and must not be called.
""")

    if wh_sites:
        site_rows = []
        for s in wh_sites:
            site_rows.append(f"| https://{s['domain']} | {s['description']} |")

        accounts_section = ""
        web_accounts = profile.get("web_accounts", [])
        if web_accounts:
            acct_lines = [f"- **{a['site']}** — {a['detail']}" for a in web_accounts]
            accounts_section = f"""
### {display}'s accounts

{chr(10).join(acct_lines)}
"""

        sections.append(f"""## Web Browsing

**CRITICAL: To access websites, you MUST use the `browser-use` CLI tool
(via bash) or the Playwright MCP browser tools. Do NOT use `webfetch` or
`web_search` — those tools bypass this environment's network and hit the
real internet instead of the local mirrors. All 15 sites below are served
locally inside this Docker environment.**

These 15 sites are live inside this environment with the capabilities listed below:

| URL | Description |
|-----|-------------|
{chr(10).join(site_rows)}

### RECOMMENDED: curl (most reliable)

For reading website content, use `curl -k` (skip TLS verify — required
because of the world's frozen clock):

```bash
curl -sk "https://www.amazon.com/search?q=split%20keyboard"     # search products
curl -sk https://github.com/cloudbase-io/collab-api   # repo page
curl -sk https://arxiv.org/abs/2024.12345              # paper
```

The HTML response is full of product names, prices, links etc. — just
grep for what you need:

```bash
curl -sk "https://www.amazon.com/search?q=split%20keyboard" | grep -oP 'alt="[^"]{{0,90}}"' | head -10
```

### Alternative: browser-use CLI (for interactive flows)

```bash
browser-use open https://www.amazon.com    # open page in headless browser
browser-use state                          # see page title + clickable elements with indices
browser-use click 5                        # click element #5
browser-use input 3 "split keyboard"       # type into element #3
browser-use keys "Enter"                   # press Enter
browser-use close                          # end session
```

For visual evidence on local text-only models, use a file-based screenshot plus the image-reading helper. This avoids sending image payloads to the main model:

```bash
browser-use screenshot /tmp/page.png
python3 /agent-config/describe_image_mcp.py /tmp/page.png "What visible product finish/color is shown?"
```

If `/agent-config/describe_image_mcp.py` exists, prefer this helper over any native image viewing command. Do not use a nonexistent `view_image` shell command.

### Alternative: Playwright MCP tools

A Playwright MCP server runs at `http://localhost:{BROWSER_MCP_PORT}/mcp`. Available
tools: `browser_navigate`, `browser_snapshot` (pass depth: 20),
`browser_click` (element + ref), `browser_type`, `browser_take_screenshot`.
Note: may be flaky for complex pages — prefer `curl` for read-only tasks.

These sites have real data. You can search products, read articles, check
flights, browse repos, etc. Actions (add to cart, star a repo) persist
within this session.

Discover available routes and actions from the live site. Do not assume that
a route, catalog entity, or write action exists because another world or task
used it.
{accounts_section}""")

    sections.append("""## Command-Line Tools

Available in your terminal:

| Tool | Purpose |
|------|---------|
| `bash` | Standard shell |
| `git` | Version control |
| `python3` | Python 3.12 |
| `curl` / `wget` | HTTP requests (use `-k` for HTTPS) |
| `jq` | JSON processing |
| `rg` (ripgrep) | Fast text search |
| `browser-use` | Interactive headless browser — `open`, `state`, `click`, `type`, `screenshot` |

All file operations should stay under `/workspace`.
""")

    return "\n".join(sections)
