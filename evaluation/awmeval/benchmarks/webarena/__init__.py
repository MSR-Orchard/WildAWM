from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .. import web
from ..base import Benchmark
from .evaluator import ACCOUNTS, Judge, evaluator_router, word_tokenize

HERE = Path(__file__).parent
SITES = ("shopping", "shopping_admin", "reddit", "gitlab", "map", "wikipedia", "homepage")
PLACEHOLDERS = (("__GITLAB__", "gitlab"), ("__REDDIT__", "reddit"), ("__SHOPPING__", "shopping"), ("__SHOPPING_ADMIN__", "shopping_admin"), ("__WIKIPEDIA__", "wikipedia"),
                ("__MAP__", "map"), ("__HOMEPAGE__", "homepage"), ("__GITLAB_BASE__", "gitlab"))
LOGIN_SITES = ("shopping", "shopping_admin", "reddit", "gitlab")
# External services the local OpenStreetMap site loads; reachable as subresources only.
MAP_RESOURCES = ("https://routing.openstreetmap.de", "https://nominatim.openstreetmap.org", "https://tile.openstreetmap.org")
AUTHENTICATION = ("If the pre-authenticated session has expired, use only these benchmark accounts: shopping emma.lopez@gmail.com / Password.123; "
                  "shopping_admin admin / admin1234; reddit MarvelsGrantMan136 / test1234; gitlab byteblaze / hello1234. ")


def login(site: str, urls: dict, executable: str) -> list[dict]:
    """Log into one site in a separate headless browser and return its cookies (the WebArena auto-login flow)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=executable)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(120_000)
            page.set_default_navigation_timeout(120_000)
            account = ACCOUNTS[site]
            if site == "shopping":
                page.goto(f"{urls['shopping']}/customer/account/login/")
                page.get_by_label("Email", exact=True).fill(account["username"])
                page.get_by_label("Password", exact=True).fill(account["password"])
                with page.expect_response(lambda response: "/customer/account/loginPost/" in response.url, timeout=120_000) as response:
                    page.get_by_role("button", name="Sign In").click(no_wait_after=True)
                if response.value.status >= 400:
                    raise RuntimeError(f"Shopping login failed with HTTP {response.value.status}")
                page.wait_for_timeout(500)
            elif site == "reddit":
                page.goto(f"{urls['reddit']}/login")
                page.get_by_label("Username").fill(account["username"])
                page.get_by_label("Password").fill(account["password"])
                page.get_by_role("button", name="Log in").click()
            elif site == "shopping_admin":
                admin = urls["shopping_admin"]
                page.goto(admin)
                page.get_by_placeholder("user name").fill(account["username"])
                page.get_by_placeholder("password").fill(account["password"])
                with page.expect_response(lambda response: response.request.method == "POST" and response.url.rstrip("/") == admin.rstrip("/"), timeout=120_000) as response:
                    page.get_by_role("button", name="Sign in").click(no_wait_after=True)
                if response.value.status >= 400:
                    raise RuntimeError(f"Shopping-admin login failed with HTTP {response.value.status}")
                page.wait_for_timeout(500)
            elif site == "gitlab":
                gitlab = urls["gitlab"]
                response = page.goto(f"{gitlab}/users/sign_in")
                if response is None or response.status >= 400:
                    raise RuntimeError(f"GitLab login page unavailable: HTTP {response.status if response else 'no response'} at {page.url}")
                for selector, attribute, kinds in (("script[src*='/assets/webpack/runtime.']", "src", ("application/javascript", "text/javascript")),
                                                   ("link[rel='stylesheet'][href*='/assets/']", "href", ("text/css",))):
                    asset_url = urljoin(page.url, page.locator(selector).first.get_attribute(attribute))
                    asset = context.request.get(asset_url)
                    if asset.status != 200 or not any(kind in asset.headers.get("content-type", "") for kind in kinds):
                        raise RuntimeError(f"GitLab frontend asset unavailable: HTTP {asset.status} at {asset_url}")
                page.get_by_test_id("username-field").click()
                page.get_by_test_id("username-field").fill(account["username"])
                page.get_by_test_id("username-field").press("Tab")
                page.get_by_test_id("password-field").fill(account["password"])
                page.get_by_test_id("sign-in-button").click(timeout=120_000)
                page.wait_for_url(lambda url: "/users/sign_in" not in str(url), timeout=120_000)
                response = context.request.get(f"{gitlab}/api/v4/user")
                if response.status != 200 or response.json().get("username") != account["username"]:
                    raise RuntimeError(f"GitLab login did not authenticate the benchmark account: HTTP {response.status}")
            return context.storage_state()["cookies"]
        finally:
            browser.close()


def on_origin(url: str, start_url: str) -> bool:
    return url.startswith("about:") or web.origin(url) == web.origin(start_url.split(" |AND| ", 1)[0])


async def recover(browser: web.Browser, start_url: str) -> dict:
    """Close off-origin tabs or navigate back to the start page before an observation (up to three attempts)."""
    first, blocked = start_url.split(" |AND| ", 1)[0], None
    for _ in range(3):
        pages = browser.context.pages
        outside, inside = [page for page in pages if not on_origin(page.url, start_url)], [page for page in pages if on_origin(page.url, start_url)]
        candidate = None
        if outside and inside:
            candidate = outside[0].url
            for page in outside:
                await page.close()
            browser.current = inside[0]
        else:
            page = await browser.page()
            if not on_origin(page.url, start_url):
                candidate = page.url
                await page.goto(first)
        blocked = blocked or candidate
        if candidate:
            await asyncio.sleep(0.25)
        page = await browser.page()
        if on_origin(page.url, start_url):
            return {"blocked_url": blocked}
        blocked = blocked or page.url
        await page.goto(first)
        await asyncio.sleep(0.25)
    return {"blocked_url": blocked, "url": first}


async def open_start(browser: web.Browser, start_url: str, cookies: list[dict] | None) -> None:
    if cookies:
        await browser.context.add_cookies(cookies)
        now = time.time()
        expected = {(cookie["name"], cookie["domain"], cookie.get("path") or "/") for cookie in cookies if not (0 < cookie.get("expires", -1) <= now)}
        actual = {(cookie["name"], cookie["domain"], cookie.get("path") or "/") for cookie in await browser.context.cookies()}
        if expected - actual:
            raise RuntimeError(f"cookie restore is missing {len(expected - actual)} of {len(expected)} cookies")
    page = await browser.page()
    urls = [url.strip() for url in start_url.split(" |AND| ")]
    await page.goto(urls[0], timeout=120_000)
    if len(urls) > 1:
        await asyncio.sleep(2)
        for url in urls[1:]:
            await (await browser.context.new_page()).goto(url, timeout=120_000)
            await asyncio.sleep(2)
        await page.bring_to_front()
        browser.current = page


async def final_state(browser: web.Browser, path: Path, start_url: str) -> tuple[str, str | None]:
    blocked = (await recover(browser, start_url))["blocked_url"]
    page = await browser.page()
    await browser.screenshot(page, path)
    return page.url, blocked


def select_page(contexts, preferred_url: str):
    pages = [page for context in contexts for page in context.pages]
    for page in reversed(pages):
        if preferred_url and page.url.rstrip("/") == preferred_url.rstrip("/"):
            return page
    return pages[-1] if pages else None


class WebArena(Benchmark):
    name = "webarena"
    metric = "pass_rate"
    authentication = AUTHENTICATION

    def __init__(self, cfg):
        super().__init__(cfg)
        self.urls = {site: str(self.options.urls[site]).rstrip("/") for site in SITES}
        self.cookies: dict[str, list[dict]] = {}
        self.login_lock = threading.Lock()

    def prepare(self) -> None:
        word_tokenize("ready")
        print(f"WebArena: {len(self.tasks())} tasks; sites: {json.dumps(self.urls)}")

    def tasks(self) -> list[dict]:
        tasks = [json.loads(line) for line in (HERE / "tasks.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        overrides = json.loads((HERE / "overrides.json").read_text(encoding="utf-8"))
        by_id = {str(task["task_id"]): task for task in tasks}
        if set(overrides) - set(by_id):
            raise ValueError(f"evaluator overrides reference unknown task ids: {sorted(set(overrides) - set(by_id), key=int)}")
        for task_id, override in overrides.items():
            by_id[task_id]["eval"].update(override)
        raw = json.dumps(tasks)
        for placeholder, site in PLACEHOLDERS:
            raw = raw.replace(placeholder, self.urls[site])
        raw = raw.replace("__GITLAB_SSH_HOST__", urlsplit(self.urls["gitlab"]).hostname or "")
        tasks = json.loads(raw)
        for task in tasks:
            task["id"] = str(task["task_id"])
            base = self.urls[task["sites"][0]] if len(task["sites"]) == 1 else None
            for url in task["start_url"].split(" |AND| "):
                if base is None or url.strip() != base and not url.strip().startswith((f"{base}/", f"{base}?", f"{base}#")):
                    raise ValueError(f"task {task['id']} start_url {url!r} is not under its site base {base}")
        return tasks

    def login(self, site: str, executable: str) -> list[dict] | None:
        """Cookies of one benchmark account, logged in once per run."""
        if site not in LOGIN_SITES:
            return None
        with self.login_lock:
            if site not in self.cookies:
                self.cookies[site] = login(site, self.urls, executable)
            return self.cookies[site]

    def run_task(self, task: dict, out: Path) -> dict:
        out, site, start_url, executable = out.resolve(), task["sites"][0], task["start_url"], web.chromium(self.options)
        allowed = web.origin(start_url.split(" |AND| ", 1)[0])
        cookies = self.login(site, executable)
        resources = MAP_RESOURCES if web.origin(self.urls["map"]) == allowed else ()
        hosts = list(dict.fromkeys(["localhost", "127.0.0.1", *(urlsplit(url).hostname for url in self.urls.values()), *(urlsplit(url).hostname for url in resources)]))
        final_screenshot = out / "screenshots" / "final.png"
        browser = web.Browser(executable, args=["--host-resolver-rules=MAP * ~NOTFOUND, " + ", ".join(f"EXCLUDE {host}" for host in hosts)])
        try:
            browser.call(open_start(browser, start_url, cookies), 300)
            agent, steps, capture = web.run_episode(self, out, browser, task_text=task["task"], start_url=start_url, allowed_origins=[allowed], resource_origins=resources,
                                                    allowed_hosts=",".join(dict.fromkeys(["127.0.0.1", "localhost", urlsplit(allowed).hostname])),
                                                    recover=lambda browser: recover(browser, start_url))
            final_url, blocked = browser.call(final_state(browser, final_screenshot, start_url), 120)
            browser.disconnect()
            agent.pop("events")
            answer = agent["final_text"] or agent["completion_summary"] or ""
            if agent["completed"]:
                steps.append(web.done_step(steps, capture["thinking"], answer, {"screenshot_path": str(final_screenshot), "url": final_url}))
            (out / "trajectory.json").write_text(json.dumps(steps, ensure_ascii=False, indent=1), encoding="utf-8")
            summary = {"site": site, "final_url": final_url, "blocked_navigation": blocked, "turns": agent["turns"], "stop_reason": agent["stop_reason"], "completed": agent["completed"],
                       "observations": {key: value for key, value in capture.items() if key != "thinking"}}
            if agent["stop_reason"] == "timeout":
                evidence = web.infrastructure_evidence(steps)
                if evidence:
                    raise RuntimeError("policy deadline reached after service failures: " + "; ".join(evidence[:3]))
                return {"score": 0.0, "concluded": "agent_error: the agent used its whole policy deadline", **summary}
            for step in steps:
                url = str(step["observation"].get("url") or "")
                if url and not url.startswith("about:") and web.origin(url.lower()) != allowed.lower():
                    raise RuntimeError(f"WebArena trajectory left the benchmark origin: {url}")
            score, results = self.evaluate(browser.cdp_url, task, final_url, answer)
        finally:
            browser.close()
        (out / "evaluation.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"score": score, **summary}

    def evaluate(self, cdp_url: str, task: dict, final_url: str, answer: str) -> tuple[float, list[dict]]:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(cdp_url)
            try:
                page = select_page(browser.contexts, final_url)
                if page is None:
                    raise RuntimeError("no page left for evaluation")
                evaluator = evaluator_router(task["eval"], task["task"], answer, page, Judge(self.cfg.judge), self.urls)
                score = evaluator()
                return float(score) if score else 0.0, evaluator.get_eval_results()
            finally:
                browser.close()
