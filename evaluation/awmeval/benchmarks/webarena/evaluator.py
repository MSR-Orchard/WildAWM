"""WebArena evaluators (string_match, url_match, program_html) with LLM fuzzy matching, adapted from the WebArena harness."""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from functools import partial

import json_repair

from .. import web

log = logging.getLogger("awmeval")
ACCOUNTS = {
    "reddit": {"username": "MarvelsGrantMan136", "password": "test1234"},
    "gitlab": {"username": "byteblaze", "password": "hello1234"},
    "shopping": {"username": "emma.lopez@gmail.com", "password": "Password.123"},
    "shopping_admin": {"username": "admin", "password": "admin1234"},
    "shopping_site_admin": {"username": "admin", "password": "admin1234"},
}
_punkt = threading.Lock()


class EvaluatorConfigError(ValueError):
    """An evaluator target is syntactically invalid."""


def word_tokenize(text: str) -> list[str]:
    import nltk
    with _punkt:
        try:
            nltk.data.find("tokenizers/punkt_tab")
        except LookupError:
            nltk.download("punkt_tab", quiet=True)
    return nltk.word_tokenize(text)


def robust_json_loads(text: str):
    if text.strip() == "":
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    decoded = json_repair.loads(text.strip())
    return {} if decoded == "" else decoded


def rest(url: str, *, data: dict | None = None, token: str | None = None):
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    request = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=120) as response:
        assert response.status == 200
        return json.loads(response.read())


def shopping_get_auth_token(urls: dict) -> str:
    return rest(f"{urls['shopping']}/rest/default/V1/integration/admin/token", data={key: ACCOUNTS["shopping_site_admin"][key] for key in ("username", "password")})


def shopping_get_order_url_from_page(page) -> str:
    """The order of this browser's checkout, never another worker's latest order."""
    if "/sales/order/view/order_id/" in page.url:
        return page.url
    links = page.locator("a[href*='/sales/order/view/order_id/']")
    if links.count() == 0:
        return page.url
    href = links.first.get_attribute("href")
    return urllib.parse.urljoin(page.url, href) if href else page.url


def shopping_admin_get_rule_url(urls: dict, page, rule_name: str) -> str:
    """Newest persisted Cart Price Rule edit URL with an exact case-insensitive name."""
    page.goto(f"{urls['shopping_admin']}/sales_rule/promo_quote/")
    rows, matches = page.locator("table.data-grid tbody tr"), []
    for index in range(rows.count()):
        row = rows.nth(index)
        if row.locator("td[data-column='name']").inner_text().strip().casefold() != rule_name.strip().casefold():
            continue
        target = row.get_attribute("title")
        if target:
            matches.append((int(row.locator("td[data-column='rule_id']").inner_text().strip()), target))
    return max(matches, key=lambda match: match[0])[1] if matches else page.url


def shopping_get_sku_latest_review_author(urls: dict, sku: str) -> str:
    reviews = rest(f"{urls['shopping']}/rest/V1/products/{sku}/reviews", token=shopping_get_auth_token(urls))
    return reviews[-1]["nickname"] if reviews else ""


def shopping_get_sku_latest_review_rating(urls: dict, sku: str) -> str:
    reviews = rest(f"{urls['shopping']}/rest/V1/products/{sku}/reviews", token=shopping_get_auth_token(urls))
    if not reviews:
        return ""
    assert reviews[0]["ratings"][0]["rating_name"] == "Rating"
    return str(reviews[-1]["ratings"][0]["percent"])


def reddit_get_post_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    parts = parsed.path.split("/")
    if len(parts) < 4 or parts[1] != "f":
        return url
    return f"{parsed.scheme}://{parsed.netloc}/f/{parts[2]}/{parts[3]}/"


def gitlab_get_project_memeber_role(page, account_name: str) -> str:
    try:
        index = page.evaluate(f"""(() => {{
            const elements = document.querySelectorAll("td[data-label='Account'] span.gl-avatar-labeled-sublabel");
            for (let i = 0; i < elements.length; i++) if (elements[i].outerText === '@{account_name}') return i;
            return -1;
        }})()""")
        return page.evaluate(f"""(() => document.querySelectorAll("td.col-max-role span")[{index}].outerText)()""")
    except Exception:
        return ""


def helpers(urls: dict, page) -> dict:
    """Names available to `func:` targets and locators."""
    return {"page": page, "shopping_get_order_url_from_page": shopping_get_order_url_from_page, "shopping_admin_get_rule_url": partial(shopping_admin_get_rule_url, urls),
            "shopping_get_sku_latest_review_author": partial(shopping_get_sku_latest_review_author, urls),
            "shopping_get_sku_latest_review_rating": partial(shopping_get_sku_latest_review_rating, urls), "reddit_get_post_url": reddit_get_post_url,
            "gitlab_get_project_memeber_role": gitlab_get_project_memeber_role}


class Judge:
    """Matcher requests at temperature 0 with the judge output budget; temperature is dropped when the model rejects it."""

    def __init__(self, section):
        self.section = section

    def request(self, messages: list[dict], json_mode: bool = False) -> str:
        body = {"model": self.section.model, "messages": messages, "max_completion_tokens": int(self.section.max_output_tokens), "temperature": 0.0}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            response = web.chat(self.section, body)
        except Exception as error:
            if "temperature" not in str(error).lower():
                raise
            body.pop("temperature")
            response = web.chat(self.section, body)
        content = ((response.get("choices") or [{}])[0].get("message") or {}).get("content")
        if not isinstance(content, str):
            raise ValueError(f"judge returned no message content: {json.dumps(response)[:500]}")
        return content


class Evaluator:
    def __init__(self, eval_config: dict, task: str, last_action_summary: str, page, judge: Judge, urls: dict):
        self.eval_config, self.task, self.last_action_summary, self.page, self.judge, self.urls = eval_config, task, last_action_summary, page, judge, urls
        self.eval_details = []
        self._last_llm_response = None

    def __call__(self) -> float:
        raise NotImplementedError

    def llm_fuzzy_match(self, pred: str, reference: str, question: str) -> float:
        message = f"""You are grading a student's answer for semantic equivalence.

Task:
Decide if the student's answer is (a) semantically equivalent to the reference answer, or (b) fully contains the reference answer with no contradictions. Ignore wording, phrasing, order, and surface-level differences. Focus only on meaning.

Special rule for "N/A":
- The string "N/A" means "not achievable".
- If and only if the reference answer is "N/A", then a correct student answer can be either "N/A" or a clear, valid reason why the task is not achievable.
- If the reference is not "N/A", then a student answer of "N/A" (or excuses) is incorrect.

Judgment labels (choose exactly one):
- correct
- partially correct
- incorrect

Guidelines:
- correct: Student's answer matches the reference meaning or fully contains all essential information from the reference without introducing contradictions.
- partially correct: Student's answer captures some but not all essential information, or mixes correct info with omissions/uncertainties (no hard contradiction with the reference).
- incorrect: Student's answer conflicts with the reference, misses essential information entirely, or relies on excuses when the reference is not "N/A".

Now grade the student's answer for this item:

question={question}
reference answer={reference}
student answer={pred}

Respond in the following json format:
{{
    "analysis": "step-by-step analysis of the grading process",
    "judgment": "exactly one of 'correct', 'partially correct', or 'incorrect'",
}}
RETURN ONLY THE DICTIONARY I ASKED FOR WITHOUT ANY COMMENTARY."""
        response_text = self.judge.request([{"role": "user", "content": message}], json_mode=True).lower()
        try:
            response_data = robust_json_loads(response_text)
        except Exception:
            response_data = {}
        response_str = response_data.get("judgment", response_text)
        self._last_llm_response = response_text
        if "incorrect" in response_str:
            return 0.0
        if "partially correct" in response_str:
            return 0.0
        if "correct" in response_str:
            return 1.0
        log.error("unexpected LLM fuzzy match response: %s", response_str)
        return 0.0

    def llm_ua_match(self, pred: str, reference: str, question: str) -> float:
        message = (
            f"task: {question}\n"
            f"actual unachievable reason: {reference}\n"
            f"reported unachievable reason: {pred}\n"
            "The task described above is inherently unachievable due to the reason specified under 'actual unachievable reason'. "
            "An individual previously attempted this task and was unable to complete it. They provided a reason for their failure, "
            "which is listed under 'reported unachievable reason'. Your role is to review both the actual and reported reasons. "
            "Determine if the reported reason aligns with the actual reason, even if implicitly. "
            "If the stated reason is in line with the actual reason, respond with 'same'. Otherwise, respond with 'different'."
        )
        response_str = self.judge.request([{"role": "system", "content": "You are a helpful assistant"}, {"role": "user", "content": message}]).lower()
        self._last_llm_response = response_str
        if "different" in response_str:
            return 0.0
        if "same" in response_str:
            return 1.0
        return 0.0


class StringEvaluator(Evaluator):
    @staticmethod
    def clean_answer(answer: str) -> str:
        answer = answer.strip()
        if answer.startswith("'") and answer.endswith("'") or answer.startswith('"') and answer.endswith('"'):
            answer = answer[1:-1]
        return answer.lower()

    @staticmethod
    def exact_match(ref: str, pred: str, question: str | None = None) -> float:
        return float(StringEvaluator.clean_answer(pred) == StringEvaluator.clean_answer(ref))

    @staticmethod
    def must_include(ref: str, pred: str, tokenize: bool = False) -> float:
        clean_pred = StringEvaluator.clean_answer(pred)
        for alternative in ref.split(" |OR| "):
            clean_ref = StringEvaluator.clean_answer(alternative)
            if tokenize and len(clean_ref) == 1 and len(word_tokenize(clean_ref)) == 1:
                if clean_ref in word_tokenize(clean_pred):
                    return 1.0
            elif clean_ref in clean_pred:
                return 1.0
        return 0.0

    def __call__(self) -> float:
        pred, question, score = self.clean_answer(self.last_action_summary), self.task, 1.0
        for approach, value in self.eval_config["reference_answers"].items():
            if approach == "exact_match":
                self._last_llm_response = None
                sub_score = self.exact_match(ref=value, pred=pred, question=question)
                score *= sub_score
                self.eval_details.append({"method": "exact_match", "input": pred, "reference": value, "score": sub_score, "llm_response": self._last_llm_response})
            elif approach == "must_include":
                assert isinstance(value, list)
                for must_value in value:
                    sub_score = self.must_include(ref=must_value, pred=pred, tokenize=len(value) == 1)
                    score *= sub_score
                    self.eval_details.append({"method": "must_include", "input": pred, "reference": must_value, "score": sub_score})
            elif approach == "must_exclude":
                assert isinstance(value, list)
                for excluded in value:
                    sub_score = 0.0 if self.clean_answer(excluded) in pred else 1.0
                    score *= sub_score
                    self.eval_details.append({"method": "must_exclude", "input": pred, "reference": excluded, "score": sub_score})
            elif approach == "regex_match":
                assert isinstance(value, list)
                for pattern in value:
                    sub_score = float(re.search(pattern, pred, re.IGNORECASE) is not None)
                    score *= sub_score
                    self.eval_details.append({"method": "regex_match", "input": pred, "reference": pattern, "score": sub_score})
            elif approach == "fuzzy_match":
                if value == "N/A":
                    self._last_llm_response = None
                    sub_score = self.exact_match(ref=value, pred=pred, question=question)
                    score *= sub_score
                    self.eval_details.append({"method": "fuzzy_match_na_exact", "input": pred, "reference": value, "score": sub_score, "llm_response": self._last_llm_response})
                    if score != 1:
                        self._last_llm_response = None
                        score = 1.0 * self.llm_ua_match(pred=pred, reference=self.eval_config["string_note"], question=self.task)
                        self.eval_details.append({"method": "fuzzy_match_na_ua", "input": pred, "reference": self.eval_config["string_note"], "score": score, "llm_response": self._last_llm_response})
                else:
                    assert isinstance(value, list)
                    for reference in value:
                        self._last_llm_response = None
                        sub_score = self.llm_fuzzy_match(reference=reference, pred=pred, question=question)
                        score *= sub_score
                        self.eval_details.append({"method": "fuzzy_match", "input": pred, "reference": reference, "score": sub_score, "llm_response": self._last_llm_response})
        return score


class URLEvaluator(Evaluator):
    def __call__(self) -> float:
        def parse(url: str) -> tuple[str, dict[str, list[str]]]:
            parsed = urllib.parse.urlparse(url)
            return parsed.netloc + parsed.path, {key: [value.strip() for value in values] for key, values in urllib.parse.parse_qs(parsed.query).items()}

        pred = str(self.page.url).rstrip("/")
        references = [str(url).rstrip("/") for url in self.eval_config["reference_url"].split(" |OR| ")]
        rule = self.eval_config.get("url_note", "GOLD in PRED")
        if rule == "EXACT":
            score = float(pred in references)
        elif rule == "GOLD in PRED":
            pred_path, pred_query, score = *parse(pred), 0.0
            for reference in references:
                ref_path, ref_query = parse(reference)
                query_score = 1.0
                for key, values in ref_query.items():
                    query_score *= float(any(value in pred_query.get(key, []) for value in values))
                score = max(score, float(ref_path in pred_path) * query_score)
        else:
            raise ValueError(f"Unknown matching rule: {rule}")
        self.eval_details.append({"method": rule, "input": pred, "reference": references, "score": score})
        return score


class HTMLContentEvaluator(Evaluator):
    def __call__(self) -> float:
        score = 1.0
        for target in self.eval_config["program_html"]:
            target_url, locator = self._resolve_target_url(target["url"]), target["locator"]
            if target_url != "last" and self.page.url.rstrip("/") != target_url.rstrip("/"):
                self.page.goto(target_url, wait_until="domcontentloaded")
                time.sleep(3)
            wait_timeout, poll_interval = float(target.get("wait_timeout", 0)), float(target.get("poll_interval", 1))
            deadline = time.monotonic() + wait_timeout
            while True:
                selected = html.unescape(str(self._extract_selected_element(target, locator) or ""))
                target_score, details = self._score_selected_element(target, target_url, locator, selected)
                if target_score == 1.0 or time.monotonic() >= deadline:
                    break
                time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
            score *= target_score
            self.eval_details.extend(details)
        return score

    def _resolve_target_url(self, configured: str) -> str:
        if not configured.startswith("func:"):
            return configured
        function = configured.split("func:", 1)[1].replace("__last_url__", self.page.url).replace("__page__", "page")
        return str(eval(function, helpers(self.urls, self.page)))

    def _extract_selected_element(self, target: dict, locator: str):
        if not locator.strip():
            return self.page.content()
        if locator.startswith("document.") or locator.startswith("[...document."):
            try:
                for action in target.get("prep_actions", []):
                    self.page.evaluate(f"() => {action}")
            except Exception:
                pass
            try:
                return self.page.evaluate(f"() => {locator}")
            except Exception as error:
                text = str(error).lower()
                if "not a valid selector" in text or "syntaxerror" in text:
                    raise EvaluatorConfigError(f"Invalid evaluator locator {locator!r}: {error}") from error
                return ""
        if locator.startswith("func:"):
            return eval(locator.split("func:", 1)[1].replace("__page__", "page"), helpers(self.urls, self.page))
        raise ValueError(f"Unknown locator: {locator}")

    def _score_selected_element(self, target: dict, target_url: str, locator: str, selected: str) -> tuple[float, list[dict]]:
        required, details, target_score = target["required_contents"], [], 1.0
        if "exact_match" in required:
            reference = required["exact_match"]
            target_score = StringEvaluator.exact_match(ref=reference, pred=selected)
            details.append({"method": "exact_match", "target_url": target_url, "locator": locator, "input": selected[:2000], "reference": reference, "score": float(target_score)})
        elif "must_include" in required:
            assert isinstance(required["must_include"], list)
            for content in required["must_include"]:
                alternatives = content.split(" |OR| ")
                current = float(any(StringEvaluator.must_include(ref=alternative, pred=selected, tokenize=False) for alternative in alternatives))
                target_score *= current
                details.append({"method": "must_include", "target_url": target_url, "locator": locator, "input": selected[:2000], "reference": alternatives, "score": current})
        elif "fuzzy_match" in required:
            assert isinstance(required["fuzzy_match"], list)
            self._last_llm_response = None
            target_score = self.llm_fuzzy_match(reference=", ".join(required["fuzzy_match"]), pred=selected, question=self.task)
            details.append({"method": "fuzzy_match", "target_url": target_url, "locator": locator, "input": selected[:2000], "reference": required["fuzzy_match"],
                            "score": target_score, "llm_response": self._last_llm_response})
        else:
            raise ValueError(f"Unknown required_contents: {required.keys()}")
        return float(target_score), details


class EvaluatorComb:
    TYPES = {"StringEvaluator": "string_match", "URLEvaluator": "url_match", "HTMLContentEvaluator": "program_html"}

    def __init__(self, evaluators: list[Evaluator]):
        self.evaluators, self.evaluator_scores = evaluators, []

    def __call__(self) -> int:
        score, self.evaluator_scores = 1.0, []
        for evaluator in self.evaluators:
            current = evaluator()
            self.evaluator_scores.append(current)
            score *= current
        return int(score)

    def get_eval_results(self) -> list[dict]:
        return [{"eval_type": self.TYPES[type(evaluator).__name__], "score": float(self.evaluator_scores[index]) if index < len(self.evaluator_scores) else 0.0,
                 "details": evaluator.eval_details} for index, evaluator in enumerate(self.evaluators)]


def evaluator_router(eval_config: dict, task: str, last_action_summary: str, page, judge: Judge, urls: dict) -> EvaluatorComb:
    kinds = {"string_match": StringEvaluator, "url_match": URLEvaluator, "program_html": HTMLContentEvaluator}
    for kind in eval_config["eval_types"]:
        if kind not in kinds:
            raise ValueError(f"eval_type {kind} is not supported")
    return EvaluatorComb([kinds[kind](eval_config, task, last_action_summary, page, judge, urls) for kind in eval_config["eval_types"]])
