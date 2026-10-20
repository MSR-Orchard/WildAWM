"""Generate persona profiles and gate them on diversity against accepted personas."""
from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from wildawm import const_config as cfg
from wildawm.synthesis.worlds import app_library as app_lib
from wildawm.synthesis.worlds import primitives as pri
from wildawm.synthesis.worlds import runtime as rt
from wildawm.webharbor import lib as wh_lib

CONSTITUTION = Path(__file__).resolve().parent / "constitutions" / "profile_creator.md"


class DiversityInfrastructureError(RuntimeError):
    """The persona diversity judge failed or returned no valid verdict."""


def profile_dir(profile_id: str) -> Path:
    return cfg.WS_PROFILES_DIR / profile_id


def list_profiles() -> list[dict]:
    """All existing persona profiles with their signatures."""
    out = []
    if not cfg.WS_PROFILES_DIR.is_dir():
        return out
    for d in sorted(cfg.WS_PROFILES_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_staging") or d.name.startswith("."):
            continue
        sig_p = d / "signature.json"
        persona_p = d / "persona.md"
        if not persona_p.exists():
            continue
        sig = {}
        if sig_p.exists():
            try:
                sig = json.loads(sig_p.read_text())
            except ValueError:
                sig = {}
        out.append({"id": d.name, "area": sig.get("area", "?"), "sig": sig,
                     "persona_text": persona_p.read_text(errors="replace")})
    return out


_JUDGE_PROMPT = """You are a strict diversity judge for synthetic personas. Decide whether the NEW persona is sufficiently DIFFERENT from all EXISTING personas. Two personas are too similar if they share the same occupation + life-domain, the same core tool/site usage pattern, or the same narrative arc — even with different names. Different names + same job + same sites = reject.

EXISTING PERSONAS:
{priors}

NEW PERSONA:
{candidate}

Reply with a single JSON object, nothing else:
{{"accept": true|false, "reason": "if rejecting, the concrete overlap (shared job/sites/arc); if accepting, the one-line differentiator"}}
Accept only when the new persona is clearly distinct. If uncertain, reject."""


def _llm_diversity_judge(new_sig: dict, new_text: str, priors: list[dict]) -> tuple[bool, str]:
    if not priors:
        return True, "no priors"
    priors_txt = "\n---\n".join(
        f"[{p['id']}] signature={json.dumps(p.get('sig') or {}, sort_keys=True)}\n{p['persona_text'][:2000]}"
        for p in priors)
    candidate = (
        f"signature={json.dumps(new_sig, sort_keys=True)}\n"
        f"{new_text[:2400]}"
    )
    prompt = _JUDGE_PROMPT.format(priors=priors_txt, candidate=candidate)
    failures: list[str] = []
    for attempt in range(1, 4):
        try:
            out = rt.run_host_judge(
                prompt, role="diversity_judge",
                timeout=cfg.LLM_SHORT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            continue
        match = re.search(r"\{.*\}", out, re.S)
        if not match:
            failures.append(f"attempt {attempt}: no JSON object: {out[:120]}")
            continue
        try:
            decision = json.loads(match.group(0))
        except ValueError as exc:
            failures.append(f"attempt {attempt}: invalid JSON: {exc}")
            continue
        if not isinstance(decision, dict) or not isinstance(decision.get("accept"), bool):
            failures.append(f"attempt {attempt}: missing boolean accept")
            continue
        return decision["accept"], str(decision.get("reason") or "")
    raise DiversityInfrastructureError(
        "persona diversity judge failed after retries: " + "; ".join(failures))


def diversity_gate(new_sig: dict, new_persona_text: str,
                   priors: list[dict]) -> tuple[bool, str]:
    return _llm_diversity_judge(new_sig, new_persona_text, priors)


@dataclass
class ProfileResult:
    profile_id: str
    accepted: bool
    diversity: tuple[bool, str]
    path: str
    iterations: int


def _done_check_profile(stage_dir: Path):
    def _check() -> tuple[bool, str]:
        for f in ("persona.md", "accounts.json", "signature.json"):
            if not (stage_dir / f).exists() or (stage_dir / f).stat().st_size == 0:
                return False, f"{f} missing/empty"
        try:
            accs = json.loads((stage_dir / "accounts.json").read_text())
            for a in accs:
                if not pri.verify_password(a["password"], a["password_bcrypt"]):
                    return False, f"account {a.get('site')} password_bcrypt does not verify"
            signature = json.loads((stage_dir / "signature.json").read_text())
            mcp_apps = signature.get("mcp_apps")
            if not isinstance(mcp_apps, list) or not 4 <= len(mcp_apps) <= 8:
                return False, "signature.mcp_apps must contain 4-8 services grounded in concrete multi-day activities"
            web_ids = set(wh_lib.all_sites()) | {wh_lib.dbfile_for_site(site).removesuffix(".db") for site in wh_lib.all_sites()}
            invalid = [app for app in mcp_apps if not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", str(app)) or app in web_ids]
            if invalid:
                return False, f"signature.mcp_apps contains invalid or WebHarbor ids: {invalid}"
            if len(set(mcp_apps)) != len(mcp_apps):
                return False, "signature.mcp_apps must contain unique service ids"
            persona_text = (stage_dir / "persona.md").read_text(errors="replace").lower()
            missing_apps = [app for app in mcp_apps if str(app).lower() not in persona_text]
            if missing_apps:
                return False, f"persona.md must ground every signature MCP service in a concrete activity: {missing_apps}"
            sites = signature.get("sites")
            if not isinstance(sites, list) or not 3 <= len(sites) <= 15:
                return False, "signature.sites must contain 3-15 active WebHarbor sites"
            invalid_sites = [site for site in sites if site not in set(wh_lib.all_sites())]
            if invalid_sites or len(set(sites)) != len(sites):
                return False, f"signature.sites contains invalid or duplicate site ids: {invalid_sites or sites}"
            account_sites = {str(account.get("site") or "") for account in accs if isinstance(account, dict)}
            if account_sites != set(sites):
                return False, f"accounts.json sites must exactly match signature.sites: accounts={sorted(account_sites)} signature={sorted(sites)}"
        except Exception as e:
            return False, f"malformed: {e}"
        return True, "persona + accounts + signature present and valid"
    return _check


def synthesize(name: str, *, force: bool = False, max_proposals: int = 3,
               steer: str = "") -> ProfileResult:
    """Generate a persona for ``name`` until the diversity gate accepts it; idempotent."""
    norm = pri.normalize_name(name)
    cfg.WS_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    priors = list_profiles()
    existing = [p for p in priors if p["id"].endswith(f"-{norm}") or p["sig"].get("name", "").lower() == name.lower()]
    if existing and not force:
        existing_dir = profile_dir(existing[0]["id"])
        valid, reason = _done_check_profile(existing_dir)()
        if not valid:
            raise RuntimeError(f"existing profile {existing[0]['id']} does not satisfy the current contract: {reason}; rerun with --force true")
        return ProfileResult(existing[0]["id"], True, (True, "already exists"),
                             str(existing_dir), 0)

    container = rt.ensure_creator(cfg.PROJECT_ROOT)
    constitution = CONSTITUTION
    last_reason = ""
    accepted_id = None
    accepted_div = (False, "no proposal accepted")
    iters = 0
    for i in tqdm(range(max_proposals), desc=f"profile[{norm}]"):
        iters = i + 1
        stage = cfg.WS_PROFILES_DIR / f"_staging_{norm}"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        prompt = _build_prompt(name, stage, priors, last_reason, steer=steer)
        dc = _done_check_profile(stage)
        rec = rt.run_stage_agent(
            prompt, role="profile_creator", container=container, cwd=str(stage),
            constitution=constitution, timeout=cfg.WORLD_SYN_PROFILE_TIMEOUT_S,
            max_turns=cfg.WORLD_SYN_MAX_LLM_TURNS, done_check=dc)
        if not rec.ok and not (stage / "persona.md").exists():
            last_reason = f"profile_creator failed ({rec.status}); produce a complete, valid persona."
            continue
        if not (stage / "persona.md").exists():
            last_reason = "persona.md was not written; write all three files."
            continue
        persona_text = (stage / "persona.md").read_text(errors="replace")
        sig = {}
        try:
            sig = json.loads((stage / "signature.json").read_text())
        except Exception:
            sig = {"area": "?"}
        ok, reason = diversity_gate(sig, persona_text, priors)
        if ok:
            slug = pri.profile_slug_seed(persona_text)
            pid = f"{slug}-{norm}"
            final = profile_dir(pid)
            if final.exists():
                shutil.rmtree(final)
            shutil.move(str(stage), str(final))
            accepted_id = pid
            accepted_div = (True, reason)
            break
        last_reason = f"diversity rejected: {reason}. Make the next persona deliberately different in occupation, life-domain, tool/site usage, and story arc."
        shutil.rmtree(stage, ignore_errors=True)
    rt.teardown_creator(cfg.PROJECT_ROOT)
    if accepted_id is None:
        raise RuntimeError(f"profile synthesis failed after {iters} proposals: {accepted_div[1]}")
    return ProfileResult(accepted_id, True, accepted_div, str(profile_dir(accepted_id)), iters)


def _build_prompt(name: str, stage: Path, priors: list[dict], feedback: str,
                  steer: str = "") -> str:
    prior_summary = ""
    if priors:
        # List each prior's sites so the creator can see which parts of the catalog are crowded.
        prior_summary = "Existing personas to be deliberately different from:\n" + "\n".join(
            f"- {p['id']}: area={p.get('area','?')} sites={','.join(p['sig'].get('sites') or []) or '?'} mcp_apps={','.join(p['sig'].get('mcp_apps') or []) or '?'}"
            for p in priors[:20])
        used = Counter(s for p in priors for s in (p["sig"].get("sites") or []))
        if used:
            crowded = ", ".join(f"{s} ({c})" for s, c in used.most_common(6))
            prior_summary += (
                f"\n\nSites already claimed by existing personas: {crowded}."
                "\nA persona's `sites` must be the handful this person ACTUALLY lives on — "
                "never the whole catalog. Claiming all 15 sites makes this persona "
                "indistinguishable from the others and is a defect.")
        used_apps = Counter(app for p in priors for app in (p["sig"].get("mcp_apps") or []))
        if used_apps:
            crowded_apps = ", ".join(f"{app} ({count})" for app, count in used_apps.most_common(10))
            prior_summary += (
                f"\nMCP services already common across persona roadmaps: {crowded_apps}."
                " Reuse a service when the activity semantics match, but vary the overall service set and arcs; an email-only or near-identical roadmap fails diversity.")
    app_index = app_lib.format_library_index()
    st = f"\n\nSTEERING (honor it, and make it genuine to the persona, not bolted on):\n{steer}" if steer else ""
    fb = f"\n\nPREVIOUS PROPOSAL FEEDBACK (incorporate this — be more different):\n{feedback}" if feedback else ""
    return f"""# Task: generate persona "{name}"

Write persona.md, accounts.json, and signature.json into the CURRENT DIRECTORY (`{stage}`), following your constitution exactly.

Ground every web entity in the REAL catalog by reading `webharbor_seeds/<site>/*.db` with sqlite3 for real products, courses, and hotels. A fresh synthesis run has no MCP services. Define 4-8 MCP services whose concrete uses are distributed across this persona's named multi-day arcs. Reuse a service id from the shared library when its semantics fit; otherwise name a generic service that a later stage can implement and share. This is an activity roadmap, not a request to pre-create services: each service is implemented only when a day plan first uses it. Do not inspect or assume source-tree reference apps are already available.

SHARED MCP-APPLICATION LIBRARY:
{app_index}

Make the persona specific, believable, and continuous (real job, real obligations, real relationships).

{prior_summary}{st}{fb}

Remember: produce password_bcrypt via `python3 -c "from wildawm.synthesis.worlds.primitives import hash_password; print(hash_password('<password>'))"` (fixed salt — never hand-write a hash). Verify every account with `bcrypt.checkpw` before finishing. Write all three files, then reply DONE."""
