from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from huggingface_hub import hf_hub_download

from .. import web
from ..base import Benchmark

MANIFEST = json.loads(Path(__file__).with_name("manifest.json").read_text(encoding="utf-8"))
BLOCK_HTTP_STATUSES = {401, 403, 407, 429, 451}
BLOCK_MARKERS = (
    "403 forbidden", "403 error", "error 403", "429 too many requests", "you don't have permission to access", "you do not have permission to access",
    "access denied", "the requested url was rejected", "the request could not be satisfied", "access to this page has been denied",
    "our systems have detected unusual traffic", "this request has been blocked", "your request has been blocked", "sorry, you have been blocked",
    "checking your browser before accessing", "checking if the site connection is secure", "security verification", "cloudflare ray id", "datadome",
    "press and hold to confirm you are a human", "verify you are human", "enable javascript and cookies to continue", "site maintenance in progress",
    "err_http2_protocol_error", "err_network_changed", "err_connection_reset", "err_connection_closed", "err_timed_out", "err_name_not_resolved",
    "err_internet_disconnected",
)
BLOCK_TITLES = {"access denied", "forbidden", "request blocked", "just a moment...", "attention required! | cloudflare"}
JUDGE_SYSTEM = '''You are an expert in evaluating the performance of a web navigation agent. The agent is designed to help a human user navigate a website to complete a task. Given the user's intent, the agent's action history, the final state of the webpage, and the agent's response to the user, your goal is to decide whether the agent's execution is successful or not.

There are three types of tasks:
1. Information seeking: The user wants to obtain certain information from the webpage, such as the information of a product, reviews, map info, comparison of map routes, etc. The bot's response must contain the information the user wants, or explicitly state that the information is not available. Otherwise, e.g. the bot encounters an exception and respond with the error content, the task is considered a failure. Besides, be careful about the sufficiency of the agent's actions. For example, when asked to list the top-searched items in a shop, the agent should order the items by the number of searches, and then return the top items. If the ordering action is missing, the task is likely to fail.
2. Site navigation: The user wants to navigate to a specific page. Carefully examine the bot's action history and the final state of the webpage to determine whether the bot successfully completes the task. No need to consider the bot's response.
3. Content modification: The user wants to modify the content of a webpage or configuration. Carefully examine the bot's action history and the final state of the webpage to determine whether the bot successfully completes the task. No need to consider the bot's response.

*IMPORTANT*
Format your response into two lines as shown below:

Thoughts: <your thoughts and reasoning process>
Status: "success" or "failure"'''
CONTENT_POLICY = "image processing blocked due to content policy violation"
_versions: dict[str, str] = {}


@dataclass(frozen=True)
class WebsiteBlock:
    url: str
    reason: str
    http_status: int | None
    marker: str | None
    title: str
    body_excerpt: str


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def classify_block(*, url: str, http_status: int | None, title: str, body: str) -> WebsiteBlock | None:
    title, body = compact(title), compact(body)
    if http_status in BLOCK_HTTP_STATUSES:
        return WebsiteBlock(url, "http_status", http_status, None, title[:500], body[:500])
    lowered = f"{title}\n{body}".lower()
    marker = next((candidate for candidate in BLOCK_MARKERS if candidate in lowered), None)
    title_marker = next((candidate for candidate in BLOCK_TITLES if title.lower() == candidate or title.lower().startswith((f"403 {candidate}", f"429 {candidate}"))), None)
    if marker is None and title_marker is None or len(body) > 5_000 and title_marker is None:
        return None
    return WebsiteBlock(url, "page_title" if marker is None else "page_marker", http_status, marker or title_marker, title[:500], body[:500])


def navigation_block(url: str, error: Exception) -> WebsiteBlock:
    message = compact(str(error))
    match = re.search(r"net::(ERR_[A-Z0-9_]+)", message)
    marker = match.group(1) if match else "navigation_timeout" if "timeout" in message.lower() else "navigation_error"
    return WebsiteBlock(url, "navigation_error", None, marker, "Browser navigation failed", message[:500])


def opencode_block(events: list[dict], *, fallback_url: str) -> WebsiteBlock | None:
    """Classify only the latest browser page state reported by a tool (Playwright MCP page header or browser-use state)."""
    for event in reversed(events):
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        output = part.get("state", {}).get("output") if isinstance(part.get("state"), dict) else None
        if not isinstance(output, str) or not output:
            continue
        url = re.search(r"(?m)^- Page URL:\s*(\S+)", output)
        viewport = re.match(r"(?i)(?:mode:\s*\S+\s+url:\s*\S+\s+)?viewport:\s*\d+x\d+\s+page:\s*\d+x\d+\s+scroll:", output.lstrip())
        if url is None and viewport is None:
            continue
        title = re.search(r"(?m)^- Page Title:\s*(.*)$", output)
        status = re.search(r"(?m)^- HTTP status:\s*(\d{3})\s*$", output)
        return classify_block(url=url.group(1).strip() if url else fallback_url, http_status=int(status.group(1)) if status else None,
                              title=title.group(1).strip() if title else "", body=output)
    return None


def normalize(rows: list[dict]) -> list[dict]:
    tasks, seen = [], set()
    for index, row in enumerate(rows):
        task_id, website, length = row.get("task_id"), row.get("website"), row.get("reference_length")
        text = next((row[name].strip() for name in ("task_description", "confirmed_task", "task") if isinstance(row.get(name), str) and row[name].strip()), None)
        if not isinstance(task_id, str) or re.fullmatch(r"[A-Za-z0-9_-]+", task_id) is None or task_id in seen:
            raise ValueError(f"Online-Mind2Web row {index} has an invalid or duplicate task_id")
        seen.add(task_id)
        parts = urlsplit(website) if isinstance(website, str) else None
        if parts is None or parts.scheme not in ("http", "https") or not parts.hostname or text is None or isinstance(length, bool) or not isinstance(length, int) or length <= 0:
            raise ValueError(f"Online-Mind2Web task {task_id} has invalid metadata")
        tasks.append({"id": task_id, "task": text, "start_url": website, "reference_length": length, "level": row.get("level") if isinstance(row.get("level"), str) else None})
    return tasks


def verify(tasks: list[dict]) -> None:
    """Ordered task ids and canonical task-list digest must match the pinned selection manifest."""
    if [task["id"] for task in tasks] != MANIFEST["task_ids"] or len(tasks) != MANIFEST["expected_tasks"]:
        raise ValueError("Online-Mind2Web task selection does not match the pinned manifest")
    canonical = [{"task_id": task["id"], "website": task["start_url"], "task": task["task"], "reference_length": task["reference_length"], "level": task["level"]} for task in tasks]
    digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    if digest != MANIFEST["task_list_sha256"]:
        raise ValueError(f"Online-Mind2Web task list digest mismatch: {digest}")


def chromium_version(executable: str) -> str:
    if executable not in _versions:
        output = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=60).stdout
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", output)
        if match is None:
            raise RuntimeError(f"cannot read the Chromium version of {executable}: {output!r}")
        _versions[executable] = match.group(1)
    return _versions[executable]


async def apply_identity(browser: web.Browser, page, version: str) -> None:
    """Headed Chromium request headers, client hints, timezone, and color scheme on the starting page."""
    major = version.split(".", 1)[0]
    metadata = {"brands": [{"brand": "Chromium", "version": major}, {"brand": "Not)A;Brand", "version": "24"}],
                "fullVersionList": [{"brand": "Chromium", "version": version}, {"brand": "Not)A;Brand", "version": "24.0.0.0"}],
                "fullVersion": version, "platform": "Linux", "platformVersion": "", "architecture": "x86", "model": "", "mobile": False, "bitness": "64",
                "wow64": False, "formFactors": ["Desktop"]}
    session = await browser.session(page)
    await session.send("Network.enable")
    await session.send("Network.setUserAgentOverride", {"userAgent": f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
                                                       "acceptLanguage": "en-US,en", "platform": "Linux x86_64", "userAgentMetadata": metadata})
    await session.send("Emulation.setTimezoneOverride", {"timezoneId": "America/Los_Angeles"})
    await session.send("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": "light"}]})


async def open_start(browser: web.Browser, url: str, version: str) -> WebsiteBlock | None:
    page = await browser.page()
    await apply_identity(browser, page, version)
    try:
        await page.goto(url, wait_until="commit", timeout=60_000)
    except Exception as error:
        return navigation_block(url, error)
    try:
        await page.wait_for_load_state("load", timeout=30_000)
    except Exception:
        pass
    title, body = await browser.state(page)
    return classify_block(url=page.url, http_status=None, title=title, body=body)


async def final_state(browser: web.Browser, path: Path) -> tuple[str, str, str]:
    page = await browser.page()
    await browser.screenshot(page, path)
    title, body = await browser.state(page)
    return page.url, title, body


def decode(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def step_action(step: dict) -> tuple[str, str | None, str | None, bool]:
    """Online-Mind2Web action string, thought, error, and terminal flag for one trajectory step."""
    output = decode(step["action"].get("model_output"))
    error = next((str(item["error"]) for item in map(decode, step["action"].get("results", [])) if item.get("error")), None)
    thought = output.get("thinking") if isinstance(output.get("thinking"), str) else None
    parts, done = [], None
    for action in output.get("action") if isinstance(output.get("action"), list) else []:
        if not isinstance(action, dict) or len(action) != 1:
            continue
        name, payload = next(iter(action.items()))
        if name == "done" and isinstance(payload, dict):
            done = payload
        parts.append(f"{name.upper()} {json.dumps(payload, ensure_ascii=False, sort_keys=True)}")
    if done is not None:
        return f"TASK_COMPLETE -> ANSWER: {done.get('text') if isinstance(done.get('text'), str) else ''}", thought, None, True
    action = "; ".join(parts) or "UNKNOWN ACTION"
    return f"{action}{f'; error={error}' if error else ''} | {'FAILED' if error else 'SUCCESS'}", thought, error, False


def write_artifacts(out: Path, task: dict, steps: list[dict], *, final_screenshot: Path, final_url: str, terminal_reason: str, block: WebsiteBlock | None) -> dict:
    """result.json and trajectory/NNNN.png in the Online-Mind2Web judge format."""
    folder = out / "trajectory"
    folder.mkdir(parents=True, exist_ok=True)
    history, answer, terminal = [], None, False

    def keep(source: Path) -> str:
        name = f"{len(history):04d}.png"
        try:
            os.link(source, folder / name)
        except OSError:
            shutil.copyfile(source, folder / name)
        return name

    for step in steps:
        action, thought, error, done = step_action(step)
        if done:
            answer, terminal = action.split("TASK_COMPLETE -> ANSWER:", 1)[1].strip(), True
        history.append({"step": len(history), "screenshot": keep(Path(step["observation"]["screenshot_path"])), "url": step["observation"].get("url"), "action": action,
                        "action_status": None if done else "FAILED" if error else "SUCCESS", "thought": thought})
        if done:
            break
    if block is not None or not terminal:
        answer = "Task not completed: website_block." if block is not None else f"Task not completed: {terminal_reason}."
        thought = f"Evaluator stopped because the website was inaccessible ({block.reason})." if block else None
        history.append({"step": len(history), "screenshot": keep(final_screenshot), "url": final_url, "action": f"TASK_COMPLETE -> ANSWER: {answer}", "action_status": None, "thought": thought})
    for item in history:
        web.png_size((folder / item["screenshot"]).read_bytes())
    result = {"schema_version": "online-mind2web-v2", "task": task["task"], "task_id": task["id"], "agent_final_answer": answer, "reference_length": task["reference_length"],
              "action_history": history}
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


class OnlineMind2Web(Benchmark):
    name = "online_mind2web"
    metric = "pass_rate"
    authentication = ""

    def prepare(self) -> None:
        print(f"Online-Mind2Web: {len(self.tasks())} tasks at {MANIFEST['revision']}")

    def tasks(self) -> list[dict]:
        path = hf_hub_download(MANIFEST["repository"], MANIFEST["source_file"], repo_type="dataset", revision=MANIFEST["revision"], token=self.options.hf_token or None)
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != MANIFEST["source_sha256"]:
            raise ValueError(f"{MANIFEST['source_file']} does not match the pinned digest")
        tasks = normalize(json.loads(raw))
        verify(tasks)
        return tasks

    def run_task(self, task: dict, out: Path) -> dict:
        out, start_url, executable = out.resolve(), task["start_url"], web.chromium(self.options)
        final_screenshot = out / "screenshots" / "final.png"
        browser = web.Browser(executable, args=["--disable-blink-features=AutomationControlled", "--lang=en-US"])
        agent, steps, capture = None, [], {}
        try:
            block = browser.call(open_start(browser, start_url, chromium_version(executable)), 180)
            if block is None:
                parts = (urlsplit(start_url).hostname or "").lower().split(".")
                root = ".".join(parts[-2:])
                hosts = ",".join(dict.fromkeys(filter(None, (".".join(parts), root, "www." + root))))
                agent, steps, capture = web.run_episode(self, out, browser, task_text=task["task"], start_url=start_url, allowed_hosts=hosts, viewport=True)
            final_url, title, body = browser.call(final_state(browser, final_screenshot), 60)
        finally:
            browser.close()
        events = agent.pop("events") if agent else []
        completed = bool(agent and agent["completed"])
        if agent and completed:
            steps.append(web.done_step(steps, capture["thinking"], agent["final_text"] or agent["completion_summary"] or "",
                                       {"screenshot_path": str(final_screenshot), "url": final_url}))
        if block is None:
            block = classify_block(url=final_url, http_status=None, title=title, body=body)
            if block is None and not completed:
                block = opencode_block(events, fallback_url=final_url)
        terminal = "website_block" if block else "task_complete" if completed else "step_limit"
        (out / "trajectory.json").write_text(json.dumps(steps, ensure_ascii=False, indent=1), encoding="utf-8")
        write_artifacts(out, task, steps, final_screenshot=final_screenshot, final_url=final_url, terminal_reason=terminal, block=block)
        summary = {"terminal_reason": terminal, "final_url": final_url, "turns": agent["turns"] if agent else 0, "stop_reason": agent["stop_reason"] if agent else None,
                   "completed": completed, "observations": {key: value for key, value in capture.items() if key != "thinking"}}
        if block:
            return {"score": 0.0, "skipped": True, "website_block": asdict(block), **summary}
        if agent["stop_reason"] == "timeout":
            evidence = web.infrastructure_evidence(steps)
            if evidence:
                raise RuntimeError("policy deadline reached after service failures: " + "; ".join(evidence[:3]))
            return {"score": 0.0, "success": False, "concluded": "agent_error: the agent used its whole policy deadline", **summary}
        verdict = self.judge(out, task)
        return {"score": 1.0 if verdict["final_label"] == "success" else 0.0, "success": verdict["final_label"] == "success", "judge_reasoning": verdict["final_reasoning"], **summary}

    def judge(self, out: Path, task: dict) -> dict:
        """Online-Mind2Web autonomous evaluator (AgentTrek judge): action history plus the final screenshot."""
        result = json.loads((out / "result.json").read_text(encoding="utf-8"))
        final = result["action_history"][-1]["screenshot"]
        actions = "\n".join(f"{index + 1}. {step['action']}" for index, step in enumerate(result["action_history"]))
        prompt = f"User Intent: {result['task']}\n\nAction History:\n{actions}\n\nThe last snapshot of the web page is shown in the image."
        image = (out / "trajectory" / final).read_bytes()
        judge = self.cfg.judge
        request = {"model": judge.model, "messages": [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": [
            {"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64.b64encode(image).decode()}", "detail": "high"}}]}],
            "max_completion_tokens": int(judge.max_output_tokens)}
        if judge.reasoning_effort is not None:
            request["reasoning_effort"] = "none" if str(judge.reasoning_effort).casefold() == "off" else judge.reasoning_effort
        transform = None
        try:
            response = web.chat(judge, request)
        except web.JudgeError as error:
            if error.status != 400 or CONTENT_POLICY not in error.detail.casefold():
                raise
            height = web.png_size(image)[1]
            crop = web.png_top(image, min(height, max(200, height // 8)))
            request["messages"][1]["content"] = [
                {"type": "text", "text": f"{prompt}\n\nThe image is an authentic top-of-viewport crop of the final screenshot because the original page content was rejected by upstream image policy. Judge from the task-relevant final-state evidence visible in this crop and the action history."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64.b64encode(crop).decode()}", "detail": "high"}}]
            response = web.chat(judge, request)
            transform = {"kind": "top_of_viewport_crop", "source_sha256": hashlib.sha256(image).hexdigest(), "derived_sha256": hashlib.sha256(crop).hexdigest(),
                         "crop_box": [0, 0, web.png_size(crop)[0], web.png_size(crop)[1]], "reason": "upstream_image_content_policy"}
        content = web.message_text(response)
        status = re.search(r"^Status\s*:\s*[\"']?(success|failure)[\"']?\s*$", content, re.MULTILINE | re.IGNORECASE)
        thoughts = re.search(r"^Thoughts\s*:\s*(.+?)(?=\nStatus\s*:)", content, re.DOTALL | re.MULTILINE | re.IGNORECASE)
        request["messages"][1]["content"][1]["image_url"] = {"artifact": final, "detail": "high", **({"transform": transform} if transform else {})}
        if status is None or thoughts is None:
            (out / "judge_parse_failure.json").write_text(json.dumps({"request": request, "response": response}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            raise ValueError("Autonomous evaluator response is not parsable")
        verdict = {"evaluation_method": "Autonomous_eval", "task_id": task["id"], "final_screenshot": final, "final_label": status.group(1).lower(),
                   "final_reasoning": thoughts.group(1).strip(), "final_raw": response}
        (out / "judge.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (out / "judge_requests.jsonl").write_text(json.dumps({"request": request, "response": response, "parsed": {key: verdict[key] for key in ("final_label", "final_reasoning")}},
                                                             ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        return verdict
