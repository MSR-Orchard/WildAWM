#!/usr/bin/env python3
"""Anthropic Messages to OpenAI-compatible proxy with per-step trajectory and image capture."""
import glob
import io
import json
import os
import random
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error

from wildawm.const_config import LLM_PROXY_UPSTREAM_TIMEOUT_S


# Headroom for json.dumps on deeply nested bodies; _json_dumps_safe handles the rest.
sys.setrecursionlimit(max(sys.getrecursionlimit(), 12000))

# Transient upstream statuses retried with backoff.
_TRANSIENT_CODES = {429, 499, 500, 502, 503, 504}

# Cap on cumulative retry sleep per inbound request (seconds).
_RETRY_SLEEP_BUDGET = float(os.environ.get("AWM_PROXY_RETRY_SLEEP_BUDGET", "120"))


def _urlopen_retry(make_request, timeout, tries=8, sleep_budget=None, read=True,
                   slept_box=None, drain=False):
    """urlopen with backoff on transient errors, bounded by a shared per-request sleep budget."""
    if sleep_budget is None:
        sleep_budget = _RETRY_SLEEP_BUDGET
    if slept_box is None:
        slept_box = [0.0]

    for attempt in range(tries):
        try:
            resp = urllib.request.urlopen(make_request(), timeout=timeout)
            if not read:
                return resp
            try:
                return resp.read()
            finally:
                resp.close()
        except urllib.error.HTTPError as e:
            if e.code in _TRANSIENT_CODES and attempt < tries - 1:
                if e.code == 429:
                    # Honour Retry-After if sent, else ramp toward ~60s so a per-minute quota can reset.
                    ra = None
                    try:
                        ra = e.headers.get("Retry-After") if e.headers else None
                    except Exception:
                        ra = None
                    # float() rejects some strings isdigit() accepts (e.g. superscripts); fall back to the ramp.
                    ra_wait = None
                    if ra:
                        try:
                            ra_wait = float(str(ra).strip())
                        except (ValueError, TypeError):
                            ra_wait = None
                    if ra_wait is not None and ra_wait >= 0:
                        wait = min(ra_wait, 90)
                    else:
                        wait = min(5 * (2 ** attempt), 60)
                    wait += random.uniform(0, 5)   # de-sync concurrent retries
                else:
                    wait = min(2 ** attempt, 30)
                if slept_box[0] + wait > sleep_budget:
                    if drain:
                        # Budget already spent by an earlier field-drop: retry without sleeping.
                        continue
                    sys.stderr.write(f"[proxy] upstream HTTP {e.code}; retry sleep "
                                     f"budget exhausted ({slept_box[0]:.1f}s + {wait:.1f}s "
                                     f"> {sleep_budget:.0f}s), giving up\n")
                    raise
                sys.stderr.write(f"[proxy] upstream HTTP {e.code}; retry "
                                 f"{attempt + 1}/{tries - 1} in {wait:.1f}s\n")
                time.sleep(wait)
                slept_box[0] += wait
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < tries - 1:
                wait = min(2 ** attempt, 30)
                if slept_box[0] + wait > sleep_budget:
                    if drain:
                        continue
                    sys.stderr.write(f"[proxy] upstream conn error ({e}); retry sleep "
                                     f"budget exhausted ({slept_box[0]:.1f}s + {wait}s "
                                     f"> {sleep_budget:.0f}s), giving up\n")
                    raise
                sys.stderr.write(f"[proxy] upstream conn error ({e}); retry "
                                 f"{attempt + 1}/{tries - 1} in {wait}s\n")
                time.sleep(wait)
                slept_box[0] += wait
                continue
            raise

import threading

# Every harness sends its model calls through this proxy, so LLM calls are counted here.
_CALL_LOCK = threading.Lock()
_CALL_COUNT = 0


def _bump_calls():
    global _CALL_COUNT
    # Stamp the reset epoch and bump atomically; lock order is always _TRAJ_LOCK -> _CALL_LOCK.
    with _TRAJ_LOCK:
        _REQUEST_GEN.gen = _TRAJ_GEN
        with _CALL_LOCK:
            _CALL_COUNT += 1


def _unbump_calls():
    """Undo one _bump_calls() for a POST merged into the previous logical call."""
    global _CALL_COUNT
    with _CALL_LOCK:
        if _CALL_COUNT > 0:
            _CALL_COUNT -= 1


# Persist each distinct inline image once (deduped by content hash) for archiving.
import base64 as _b64
import hashlib as _hash

IMAGE_DIR = os.environ.get("AWM_PROXY_IMAGE_DIR", "/tmp/xai_proxy_images")
_IMG_SEEN: dict = {}   # content-hash -> saved filename
_IMG_N = 0             # authoritative count (== files on disk)
_IMG_NAME_SEQ = 0      # filename sequence only; gaps are harmless
_IMG_INFLIGHT: set = set()  # hashes being written, so same-bytes writers serialize


# Extensions used verbatim; anything else falls back to a safe default.
_IMG_EXT_WHITELIST = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif",
                      "svg", "ico", "heic", "heif", "avif"}


def _sanitize_ext(media_type: str | None) -> str:
    """Derive a path-safe filename extension from a possibly adversarial media_type."""
    # A non-string media_type must not raise.
    if not isinstance(media_type, str) or "/" not in media_type:
        return "png"
    ext = media_type.split("/")[-1].split(";")[0]
    # Keep only ASCII alphanumerics so the extension is path-safe.
    ext = "".join(ch for ch in ext if ch.isalnum() and ord(ch) < 128).lower()
    if ext in _IMG_EXT_WHITELIST:
        return ext
    return "png"


def _sanitize_media_type(media_type) -> str:
    """Sanitize media_type for a data: URI by cutting it at the first ';' or ','."""
    s = media_type if isinstance(media_type, str) else ""
    s = s.split(";", 1)[0].split(",", 1)[0].strip()
    return s or "image/png"


def _save_image(b64_data: str, media_type: str | None) -> str | None:
    """Persist one inline image (deduped by hash); return its filename, or None on failure."""
    global _IMG_N, _IMG_NAME_SEQ
    # Reset epoch this request was admitted under; None means not gated.
    stamped_gen = getattr(_REQUEST_GEN, "gen", None)
    try:
        raw = _b64.b64decode(b64_data)
    except Exception:
        return None
    if len(raw) < 64:
        return None
    h = _hash.sha256(raw).hexdigest()
    ext = _sanitize_ext(media_type)

    # Reserve this hash; if another thread is writing it, wait and re-check.
    for _ in range(10000):
        with _CALL_LOCK:
            if h in _IMG_SEEN:
                return _IMG_SEEN[h]
            if h not in _IMG_INFLIGHT:
                _IMG_INFLIGHT.add(h)
                _IMG_NAME_SEQ += 1
                seq = _IMG_NAME_SEQ
                fname = f"img_{seq:03d}_{h[:8]}.{ext}"
                break
        time.sleep(0.001)
    else:
        # A same-hash writer appears stuck; return any committed result.
        with _CALL_LOCK:
            return _IMG_SEEN.get(h)

    try:
        os.makedirs(IMAGE_DIR, exist_ok=True)
        with open(os.path.join(IMAGE_DIR, fname), "wb") as f:
            f.write(raw)
    except Exception as e:
        # Write failed: release the marker only, so the image stays re-capturable.
        with _CALL_LOCK:
            _IMG_INFLIGHT.discard(h)
        sys.stderr.write(f"[proxy] image save failed (not counted): {e}\n")
        return None
    # Commit under the reset's lock order; a fenced-out straggler deletes its file instead.
    with _TRAJ_LOCK:
        if stamped_gen is not None and stamped_gen != _TRAJ_GEN:
            try:
                os.remove(os.path.join(IMAGE_DIR, fname))
            except Exception:
                pass
            with _CALL_LOCK:
                _IMG_INFLIGHT.discard(h)
            sys.stderr.write("[proxy] image dropped (fenced-out straggler, "
                             "stale reset epoch): not counted\n")
            return None
        with _CALL_LOCK:
            _IMG_SEEN[h] = fname
            _IMG_N += 1
            n = _IMG_N
            _IMG_INFLIGHT.discard(h)
    sys.stderr.write(f"[proxy] captured image #{n} ({len(raw)}B {media_type})\n")
    return fname


def _save_data_uri(u: str) -> str | None:
    try:
        # Split on ';base64,' (not the first comma) to match _data_uri_hash.
        if ";base64," in u:
            head, b64 = u.split(";base64,", 1)
        else:
            head, b64 = u.split(",", 1)
        media = head.split(":", 1)[1].split(";", 1)[0] if ":" in head else "image/png"
        return _save_image(b64, media)
    except Exception:
        return None


def _walk_images(node, sink: list | None = None):
    """Persist inline images anywhere in the request (iterative walk); append filenames to sink."""
    # Push children in reverse so they pop in document order.
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            # Per-node fault isolation: a malformed node skips only itself.
            try:
                t = cur.get("type")
                if t == "image":                                  # anthropic block
                    src = cur.get("source")
                    src = src if isinstance(src, dict) else {}
                    if src.get("type") == "base64":
                        fn = _save_image(src.get("data", ""), src.get("media_type"))
                        if fn:
                            # Saved: the bytes are this node's payload, so do not descend.
                            if sink is not None:
                                sink.append(fn)
                            continue
                        # Save failed: fall through and descend, mirroring the forward walk.
                    elif src.get("type") == "url":
                        # An inline data: URI is image bytes; save it, then still descend like the forward walk.
                        u = src.get("url", "")
                        u = u.strip() if isinstance(u, str) else u
                        if isinstance(u, str) and u.lower().startswith("data:"):
                            fn = _save_data_uri(u)
                            if fn and sink is not None:
                                sink.append(fn)
                if t in ("image_url", "input_image"):             # openai chat/responses
                    iu = cur.get("image_url", "")
                    u = iu if isinstance(iu, str) else (
                        iu.get("url", "") if isinstance(iu, dict) else "")
                    # The data: scheme is case-insensitive; strip whitespace to match the forwarded URI.
                    if isinstance(u, str):
                        u = u.strip()
                    if isinstance(u, str) and u.lower().startswith("data:"):
                        fn = _save_data_uri(u)
                        if fn:
                            if sink is not None:
                                sink.append(fn)
                            continue
                        # Save failed: still descend so nested images are captured.
            except Exception as e:                                # bad node -> skip only it
                sys.stderr.write(f"[proxy] image walk: skipping malformed node: {e}\n")
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def _scan_images(req: dict) -> list[str]:
    """Persist every inline image in a request body; return this request's filenames in walk order."""
    sink: list[str] = []
    try:
        _walk_images(req.get("messages"), sink)
        _walk_images(req.get("input"), sink)
        # A list-form system field can carry images too.
        _walk_images(req.get("system"), sink)
    except Exception as e:                                        # never break a call
        sys.stderr.write(f"[proxy] image scan error: {e}\n")
    seen: set = set()
    out: list[str] = []
    for fn in sink:
        if fn not in seen:
            seen.add(fn)
            out.append(fn)
    return out


# Per-call trajectory: one <fp>.jsonl per session (first-user-text hash), new messages only.
TRAJ_DIR = os.environ.get("AWM_PROXY_TRAJ_DIR", "/tmp/xai_proxy_traj")
_TRAJ_LOCK = threading.Lock()
_TRAJ_STEPS: dict = {}      # fp -> [step record, ...] (in-memory mirror)
_TRAJ_MAX_STEP = 0          # max step index across all sessions (for /stats)
_TRAJ_SEEDED: set = set()   # fps already seeded from their pre-restart .jsonl

# Reset epoch: steps stamped with a stale epoch (straggling across a reset) are dropped.
_TRAJ_GEN = 0                       # bumped under _TRAJ_LOCK on every reset
_REQUEST_GEN = threading.local()    # per-request stamp: the epoch read at entry


def _first_user_text(messages) -> str:
    """Text of the first user message (the session anchor), joined like anthropic_to_openai."""
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for b in c:
                if isinstance(b, dict):
                    if b.get("type") in ("text", "input_text") or "text" in b:
                        parts.append(_coerce_text_field(b.get("text")))
                else:
                    # Flatten non-dict elements like anthropic_to_openai so both endpoints hash alike.
                    parts.append(_flatten_block_text(b))
            return "\n".join(p for p in parts if p)
        return _coerce_text(c)
    return ""


# Per-process salt and counter so empty-anchor fingerprints stay unique.
_FP_SALT = _hash.sha256(os.urandom(16)).hexdigest()[:16]
_FP_EMPTY_SEQ = 0


def _is_text_only_blocks(blocks) -> bool:
    """True if every element is a pure text block (equivalent to its joined text)."""
    for b in blocks:
        if isinstance(b, str):
            continue
        if isinstance(b, dict) and b.get("type") in ("text", "input_text") \
                and set(b.keys()) <= {"type", "text", "cache_control"}:
            continue
        return False
    return True


def _fp_residue(content):
    """Structural residue of content (non-text, non-image parts) for fingerprinting."""
    # Hashed only, never sent, so raw structure is kept to keep distinct openers distinct.
    def _text_block_extra(b):
        # Non-text fields of a text block still distinguish openers.
        return {k: v for k, v in b.items() if k not in ("type", "text")}
    if isinstance(content, str) or _is_image_block(content):
        return ""
    if isinstance(content, dict):
        if content.get("type") in ("text", "input_text"):
            ex = _text_block_extra(content)
            return ex if ex else ""
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, str) or _is_image_block(b):
                continue
            if isinstance(b, dict) and b.get("type") in ("text", "input_text"):
                ex = _text_block_extra(b)
                if ex:
                    out.append(ex)
                continue
            out.append(b)
        return out if out else ""
    return _coerce_text(content)


def _norm_msg_for_fp(m):
    """Endpoint-invariant, collision-resistant projection of one message for fingerprinting."""
    if not isinstance(m, dict):
        return ["__fp_nonmsg__", _coerce_text(m)]
    # Return a list, not a dict, so a message field cannot clobber the canonical slot.
    fields = sorted(((k, _blank_image_data(v)) for k, v in m.items() if k != "content"),
                    key=lambda kv: str(kv[0]))
    c = m.get("content")
    canon = [
        _flatten_block_text(c),
        _image_markers_of_message({"content": c}),
        _fp_residue(c),
    ]
    return ["__fp_msg__", fields, canon]


def _norm_msgs_for_fp(msgs: list) -> list:
    return [_norm_msg_for_fp(m) for m in (msgs or [])]


def _empty_anchor_prefix(msgs: list) -> list:
    """Stable leading slice of messages used to fingerprint an opener with no first-user text."""
    base_end = len(msgs)
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and m.get("role") == "user":
            base_end = i + 1
            break
    base = msgs[:base_end]
    following = msgs[base_end:]
    # Normalize the base; with a saved base image, fold the whole leading image run's markers.
    norm_base = _norm_msgs_for_fp(base)
    if _prefix_has_saved_image(base):
        leading_imgs = _leading_image_run_marker(msgs)
        return (norm_base + [{"__empty_anchor_first_image__": leading_imgs}]
                if leading_imgs is not None else norm_base)
    # No saved base image: fold the leading image run of the following messages.
    leading_imgs = _leading_image_run_marker(following)
    follow_text = _following_text_marker(following)
    if leading_imgs is not None:
        out = norm_base + [{"__empty_anchor_first_image__": leading_imgs}]
        # A saved image wins over text; sub-floor-only images also fold the text marker.
        run_has_saved = any(isinstance(mk, str) and not mk.lower().startswith("data:")
                            for mk in (leading_imgs or []))
        if not run_has_saved and follow_text is not None:
            out = out + [{"__empty_anchor_following_text__": follow_text}]
        return out
    # No image: fold a marker of the following messages' text.
    if follow_text is not None:
        return norm_base + [{"__empty_anchor_following_text__": follow_text}]
    return norm_base


def _first_image_marker(msgs: list):
    """Marker for the first renderable image: its content hash, or the raw data URI if too small."""
    try:
        for uri in _iter_render_images(msgs, "data_uri"):
            if not isinstance(uri, str):
                continue
            return _data_uri_hash(uri) or uri
    except Exception:
        return None
    return None


def _image_markers_of_message(m):
    """Markers of every renderable image in one message, in document order."""
    out = []
    try:
        for uri in _iter_render_images([m], "data_uri"):
            if isinstance(uri, str):
                out.append(_data_uri_hash(uri) or uri)
    except Exception:
        return out
    return out


def _leading_image_run_marker(msgs: list):
    """Image markers of the leading non-assistant run starting at the first image, or None."""
    try:
        markers = []
        started = False
        for m in msgs:
            imgs = _image_markers_of_message(m)
            if not started:
                if imgs:
                    started = True
                    markers.extend(imgs)
                # Before the run starts, skip messages without images.
                continue
            # An assistant message ends the leading run.
            if isinstance(m, dict) and m.get("role") == "assistant":
                break
            markers.extend(imgs)
        return markers if started else None
    except Exception:
        # Fall back to the first-image marker.
        return _first_image_marker(msgs)


def _prefix_has_saved_image(msgs: list) -> bool:
    """True if any message carries an image large enough to be saved."""
    try:
        for uri in _iter_render_images(msgs, "data_uri"):
            if isinstance(uri, str) and _data_uri_hash(uri) is not None:
                return True
    except Exception:
        return False
    return False


def _following_text_marker(msgs: list):
    """Hashes of the leading non-assistant messages of an empty-anchor opener, or None."""
    def _msg_hash(m):
        # Hash the normalized projection so both endpoints produce the same marker.
        try:
            return _hash.sha256(json.dumps(_norm_msg_for_fp(m), sort_keys=True, default=str)
                                .encode("utf-8", "surrogatepass")).hexdigest()
        except Exception:
            return _hash.sha256(repr(m).encode("utf-8", "ignore")).hexdigest()
    try:
        nonassist = []   # opener-body message hashes
        assist = []      # leading assistant hashes (fallback only)
        seen_non_assistant = False
        for m in msgs:
            if not isinstance(m, dict):
                continue
            is_assistant = m.get("role") == "assistant"
            # Stop at the first assistant after the opener body, excluding grow turns.
            if is_assistant and seen_non_assistant:
                break
            if is_assistant:
                assist.append(_msg_hash(m))
            else:
                nonassist.append(_msg_hash(m))
                seen_non_assistant = True
        # Prefer the non-assistant body; leading assistant text is only a fallback.
        markers = nonassist if nonassist else assist
        return markers if markers else None
    except Exception:
        return None


def _session_fp(messages) -> str:
    """Stable 16-hex session fingerprint (first-user text hash, or a salted fallback)."""
    anchor = _first_user_text(messages)
    if anchor:
        return _hash.sha256(
            anchor.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    # Empty anchor: salted hash of the stable leading context.
    msgs = messages if isinstance(messages, list) else []
    if msgs:
        try:
            lead = json.dumps(_empty_anchor_prefix(msgs),
                              sort_keys=True, default=str)
        except Exception:
            lead = repr(_empty_anchor_prefix(msgs))
        return _hash.sha256(
            ("\x00".join(("EMPTY_ANCHOR", _FP_SALT, lead)))
            .encode("utf-8", "surrogatepass")).hexdigest()[:16]
    # Nothing to anchor on: mint a unique per-call key.
    global _FP_EMPTY_SEQ
    with _CALL_LOCK:
        _FP_EMPTY_SEQ += 1
        seq = _FP_EMPTY_SEQ
    return _hash.sha256(
        f"EMPTY_BODY\x00{_FP_SALT}\x00{seq}".encode("utf-8")).hexdigest()[:16]


def _usable_id_str(raw) -> str:
    """Tool/call id as a string, or empty so the caller mints a placeholder."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, int) and not isinstance(raw, bool):
        return str(raw)
    if isinstance(raw, float):
        # Non-finite floats have no JSON text; mint instead.
        return str(raw) if (raw == raw and raw not in (float("inf"), float("-inf"))) else ""
    return ""


def _is_usable_tool_call(tc) -> bool:
    """True if the tool_call has a name or an id (not an empty placeholder)."""
    if not isinstance(tc, dict):
        return False
    fn = tc.get("function")
    name = (fn.get("name") if isinstance(fn, dict) else None) or tc.get("name")
    if isinstance(name, str) and name.strip():
        return True
    return bool(_usable_id_str(tc.get("id")))


def _extract_output(response: dict) -> dict:
    """Assistant content and tool_calls from a chat/completions-shaped upstream response."""
    try:
        choice = (response.get("choices") or [{}])[0]
        if not isinstance(choice, dict):
            choice = {}
        msg = choice.get("message")
        # A non-dict message yields an empty step instead of raising.
        if not isinstance(msg, dict):
            msg = {}
        # Drop null, non-dict and empty tool_call elements.
        tcs = [tc for tc in (msg.get("tool_calls") or []) if _is_usable_tool_call(tc)]
        # Scalars are recorded verbatim; block content is reduced to visible text without reasoning.
        _c = msg.get("content")
        if isinstance(_c, (list, dict)):
            return {"content": _visible_text(_c), "tool_calls": tcs}
        return {"content": _c or "", "tool_calls": tcs}
    except Exception:
        return {"content": "", "tool_calls": []}


def _chat_response_summary(resp: dict) -> str:
    """Compact upstream response shape for proxy post-mortems."""
    try:
        choice = (resp.get("choices") or [{}])[0]
        if not isinstance(choice, dict):
            return "choice=non-dict"
        msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = msg.get("content")
        tool_calls = [tc for tc in (msg.get("tool_calls") or [])
                      if _is_usable_tool_call(tc)]
        if isinstance(content, str):
            c_desc = f"str:{len(content)}"
        elif content is None:
            c_desc = "none"
        else:
            c_desc = type(content).__name__
        names = []
        for tc in tool_calls[:5]:
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = fn.get("name") or tc.get("name") or "?"
            names.append(str(name))
        names_desc = ",".join(names)
        if len(tool_calls) > len(names):
            names_desc += ",..."
        usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
        usage_desc = ""
        if usage:
            usage_desc = (
                f" usage_in={usage.get('prompt_tokens', 0)}"
                f" usage_out={usage.get('completion_tokens', 0)}"
            )
        return (
            f"finish={choice.get('finish_reason')} content={c_desc} "
            f"tool_calls={len(tool_calls)}"
            f"{' names=' + names_desc if names_desc else ''}"
            f"{usage_desc}"
        )
    except Exception as e:
        return f"summary_error={type(e).__name__}:{e}"


def _public_step(rec: dict) -> dict:
    """Step record without internal bookkeeping fields (as persisted and served)."""
    return {k: v for k, v in rec.items() if not k.startswith("_")}


_REQUEST_PARAM_KEYS = (
    "model",
    "stream",
    "reasoning",
    "reasoning_effort",
    "reasoningEffort",
    "chat_template_kwargs",
    "max_tokens",
    "max_completion_tokens",
    "max_output_tokens",
    "temperature",
    "top_p",
    "tool_choice",
)
_REQUEST_PARAMS_META = "__wildawm_request_params"


def _request_params_snapshot(body: dict | None, wire_api: str | None = None) -> dict:
    """Non-sensitive top-level request parameters for trajectory auditing."""
    out = {"wire_api": wire_api or "chat"}
    if not isinstance(body, dict):
        return out
    for key in _REQUEST_PARAM_KEYS:
        if key in body:
            out[key] = _finite_sanitize(body[key])
    return out


def _attach_request_params(resp: dict, body: dict | None,
                           wire_api: str | None = None) -> dict:
    if isinstance(resp, dict):
        resp[_REQUEST_PARAMS_META] = _request_params_snapshot(body, wire_api)
    return resp


def _is_empty_output(o: dict) -> bool:
    """True if the assistant turn has no text and no tool_calls."""
    o = o or {}
    return (o.get("content") in ("", None)) and not o.get("tool_calls")


def _msg_sigs(messages) -> list:
    """Per-message content fingerprints, to tell duplicates from context compaction."""
    out = []
    for m in messages or []:
        try:
            out.append(_hash.sha256(
                json.dumps(m, sort_keys=True, default=str)
                .encode("utf-8", "surrogatepass")).hexdigest())
        except Exception:
            out.append(_hash.sha256(repr(m).encode("utf-8", "ignore")).hexdigest())
    return out


def _non_system_msg_sigs(messages) -> list:
    """Message fingerprints excluding volatile system records."""
    return _msg_sigs([
        m for m in (messages or [])
        if not (isinstance(m, dict) and m.get("role") == "system")
    ])


def _common_prefix_len(a: list, b: list) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


_TRAJ_TMP_SEQ = 0


def _seed_session_from_disk_locked(fp: str) -> None:
    """Seed the mirror from <fp>.jsonl on first touch after a restart; caller holds _TRAJ_LOCK."""
    global _TRAJ_MAX_STEP, _TRAJ_TMP_SEQ
    if fp in _TRAJ_SEEDED:
        return
    _TRAJ_SEEDED.add(fp)
    # Never overlay disk onto a live in-process session.
    if _TRAJ_STEPS.get(fp):
        return
    path = os.path.join(TRAJ_DIR, f"{fp}.jsonl")
    try:
        with open(path, "r") as f:
            raw = f.read()
    except FileNotFoundError:
        return
    except Exception as e:
        sys.stderr.write(f"[proxy] traj seed read failed for {fp}: {e}\n")
        return
    seeded = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            # Torn tail line from an interrupted append; skip it.
            continue
        if isinstance(rec, dict) and isinstance(rec.get("step"), int):
            seeded.append(rec)
    # Truncate a torn tail to the parseable prefix so the next append starts on a new line.
    if raw and not raw.endswith("\n"):
        try:
            clean = "".join(
                json.dumps(_public_step(r), ensure_ascii=True) + "\n" for r in seeded)
            _TRAJ_TMP_SEQ += 1
            tmp = os.path.join(
                TRAJ_DIR,
                f".{fp}.{os.getpid()}.{threading.get_ident()}.{_TRAJ_TMP_SEQ}.seedheal.tmp")
            with open(tmp, "w") as f:
                f.write(clean)
            os.replace(tmp, path)        # atomic: torn tail dropped, prefix preserved
        except Exception as e:
            sys.stderr.write(f"[proxy] traj seed torn-tail heal failed for {fp}: {e}\n")
    if not seeded:
        return
    # Seeded records lack _msig, so the first post-restart step appends rather than merges.
    _TRAJ_STEPS[fp] = seeded
    top = max((r.get("step") or 0) for r in seeded)
    if top > _TRAJ_MAX_STEP:
        _TRAJ_MAX_STEP = top


def _persist_session_locked(fp: str, append_rec: dict | None = None) -> None:
    """Persist <fp>.jsonl: append one step, or atomically rewrite from the mirror; hold _TRAJ_LOCK."""
    global _TRAJ_TMP_SEQ
    os.makedirs(TRAJ_DIR, exist_ok=True)
    path = os.path.join(TRAJ_DIR, f"{fp}.jsonl")
    if append_rec is not None:
        # New step: append under _TRAJ_LOCK (serialized vs merges) instead of rewriting the file.
        with open(path, "a") as f:
            f.write(_json_dumps_safe(_public_step(append_rec), ensure_ascii=True) + "\n")
        return
    lines = [_json_dumps_safe(_public_step(r), ensure_ascii=True)
             for r in _TRAJ_STEPS.get(fp, [])]
    _TRAJ_TMP_SEQ += 1
    tmp = os.path.join(
        TRAJ_DIR, f".{fp}.{os.getpid()}.{threading.get_ident()}.{_TRAJ_TMP_SEQ}.tmp")
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(tmp, path)


def _log_step(messages, response: dict, images: list | None = None,
              request_body: dict | None = None,
              wire_api: str | None = None) -> None:
    """Log one non-cumulative step record for a model call; never raises."""
    try:
        if not isinstance(messages, list):
            return
        embedded_params = None
        if isinstance(response, dict):
            embedded_params = response.pop(_REQUEST_PARAMS_META, None)
        request_params = (
            embedded_params
            if isinstance(embedded_params, dict)
            else _request_params_snapshot(request_body, wire_api)
        )
        # Epoch this call was admitted under; a later reset makes it a fenced-out straggler.
        stamped_gen = getattr(_REQUEST_GEN, "gen", None)
        fp = _session_fp(messages)
        cur_sigs = _msg_sigs(messages)
        new_out = _extract_output(response)
        merged = False
        new_rec = None
        with _TRAJ_LOCK:
            global _TRAJ_MAX_STEP
            # A stale epoch here means a reset landed since admission: drop the step.
            if stamped_gen is not None and stamped_gen != _TRAJ_GEN:
                return
            # First touch after a restart: seed the mirror from disk so a merge rewrite keeps old steps.
            _seed_session_from_disk_locked(fp)
            steps = _TRAJ_STEPS.setdefault(fp, [])
            last_sigs = (steps[-1].get("_msig") or []) if steps else []
            last_non_system_sigs = (
                steps[-1].get("_non_system_msig") or last_sigs) if steps else []
            prev = len(last_sigs)        # previous call's message count
            cur = len(cur_sigs)
            cpl = _common_prefix_len(cur_sigs, last_sigs)
            # One step per logical call: log the tail after the common prefix; on shrink, the whole body.
            if not steps:
                tail = list(messages)
            elif cur < prev:
                tail = list(messages)                                   # shrink
            else:
                tail = messages[cpl:] if 0 <= cpl <= len(messages) else list(messages)
            # True duplicate: same count and prefix, differing only in the last message.
            cur_non_system_sigs = _non_system_msg_sigs(messages)
            same_non_system = cur_non_system_sigs == last_non_system_sigs
            is_true_dup = bool(
                steps and cur == prev and (
                    cur_sigs[:-1] == last_sigs[:-1] or same_non_system))
            if is_true_dup:
                # Merge into the last step; never replace a non-empty output with an empty one.
                last = steps[-1]
                if not (_is_empty_output(new_out) and not _is_empty_output(last.get("output"))):
                    last["output"] = new_out
                    last["request_params"] = request_params
                # A duplicate adds no content; keep the step's own images.
                last["ts"] = time.time()
                # Keep the first POST's _msig so a later grow re-emits the differing last message.
                if same_non_system and cur_sigs[:-1] != last_sigs[:-1]:
                    # System-only change: merge, but advance the baseline so the next growth emits only new turns.
                    last["_msig"] = cur_sigs
                    last["_non_system_msig"] = cur_non_system_sigs
                merged = True
            else:
                # A new logical call (growth, compaction, shrink or first call); stays counted.
                step_no = len(steps) + 1
                rec = {
                    "step": step_no, "session": fp, "input": tail,
                    "output": new_out,
                    "request_params": request_params,
                    # Reuse already-saved images by content hash; re-saving the tail could write phantom files.
                    "images": _step_images_for_tail(tail) or list(images or []),
                    "ts": time.time(), "_msig": cur_sigs,
                    "_non_system_msig": cur_non_system_sigs,
                }
                steps.append(rec)
                new_rec = rec
                if step_no > _TRAJ_MAX_STEP:
                    _TRAJ_MAX_STEP = step_no
            # Persist under the same lock: append for a new step, atomic rewrite for a merge.
            try:
                _persist_session_locked(fp, append_rec=new_rec)
            except Exception as e:
                sys.stderr.write(f"[proxy] trajectory persist failed: {e}\n")
        # A merged duplicate is the same logical call: undo the optimistic call count.
        if merged:
            _unbump_calls()
    except Exception as e:                                        # never break a call
        sys.stderr.write(f"[proxy] trajectory log error: {e}\n")


def _reset_trajectory() -> None:
    """Atomically reset trajectory state, counters and on-disk archives (GET /stats?reset=1)."""
    global _TRAJ_MAX_STEP, _TRAJ_GEN, _CALL_COUNT, _IMG_N, _IMG_NAME_SEQ
    with _TRAJ_LOCK:
        _TRAJ_GEN += 1
        _REQUEST_GEN.gen = _TRAJ_GEN
        _TRAJ_STEPS.clear()
        # The archive is wiped below, so later solves must not re-seed from it.
        _TRAJ_SEEDED.clear()
        _TRAJ_MAX_STEP = 0
        # The mirror is empty here, so every stale <fp>.jsonl is removed; the guard is defensive.
        keep_files = {f"{fp}.jsonl" for fp in _TRAJ_STEPS}
        with _CALL_LOCK:
            _CALL_COUNT = 0
            _IMG_N = 0
            _IMG_NAME_SEQ = 0
            _IMG_SEEN.clear()
            _IMG_INFLIGHT.clear()
        try:
            for p in glob.glob(os.path.join(TRAJ_DIR, "*.jsonl")):
                if os.path.basename(p) in keep_files:
                    continue
                try:
                    os.remove(p)
                except Exception:
                    pass
        except Exception as e:
            sys.stderr.write(f"[proxy] trajectory reset failed: {e}\n")
        # Wipe saved images too so a recurring figure is captured once per solve.
        try:
            for p in glob.glob(os.path.join(IMAGE_DIR, "*")):
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
        except Exception as e:
            sys.stderr.write(f"[proxy] image reset failed: {e}\n")


# Opt-in raw inbound-body capture for debugging (AWM_PROXY_DEBUG_RAW=1).
_DEBUG_RAW = os.environ.get("AWM_PROXY_DEBUG_RAW", "") in ("1", "true", "yes")
DEBUG_RAW_DIR = os.environ.get("AWM_PROXY_DEBUG_RAW_DIR", "/tmp/awm_proxy_rawdbg")
_DEBUG_RAW_LOCK = threading.Lock()
_DEBUG_RAW_SEQ = 0


def _debug_raw_dump(path: str, raw: bytes, headers=None) -> None:
    if not _DEBUG_RAW:
        return
    try:
        global _DEBUG_RAW_SEQ
        with _DEBUG_RAW_LOCK:
            _DEBUG_RAW_SEQ += 1
            seq = _DEBUG_RAW_SEQ
        h = _hash.sha256(raw or b"").hexdigest()[:8]
        safe = path.strip("/").replace("/", "_") or "root"
        os.makedirs(DEBUG_RAW_DIR, exist_ok=True)
        base = os.path.join(DEBUG_RAW_DIR, f"{seq:04d}_{safe}_{h}")
        with open(base + ".body.json", "wb") as f:
            f.write(raw or b"")
        if headers is not None:
            try:
                hdrs = {k: v for k, v in headers.items()}
            except Exception:
                hdrs = {}
            with open(base + ".headers.json", "w") as f:
                json.dump(hdrs, f, indent=1, sort_keys=True)
        sys.stderr.write(f"[proxy][rawdbg] #{seq} {path} {len(raw or b'')}B sha8={h}\n")
    except Exception as e:                                        # never break a call
        sys.stderr.write(f"[proxy][rawdbg] dump failed: {e}\n")


PORT = int(os.environ.get("AWM_LLM_PROXY_PORT", "3456"))
OPENAI_BASE_URL = os.environ.get("AWM_OPENAI_BASE_URL", "")
OPENAI_API_KEY = os.environ.get("AWM_OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("AWM_OPENAI_MODEL", "gpt-4.1")
CHAT_TOKEN_LIMIT_PARAM = os.environ.get("AWM_CHAT_TOKEN_LIMIT_PARAM", "").strip()
MODEL_TEXT_ONLY = os.environ.get("AWM_MODEL_TEXT_ONLY", "").lower() in ("1", "true", "yes", "on")
# Use the request's model, else OPENAI_MODEL (forced by AWM_PROXY_PIN_MODEL); see _anthropic_model.
PIN_MODEL = os.environ.get("AWM_PROXY_PIN_MODEL", "") in ("1", "true", "yes")
PROXY_MODEL_REF = os.environ.get("AWM_PROXY_MODEL_REF", "")
REASONING_EFFORT = os.environ.get("AWM_REASONING_EFFORT", "").strip().lower()
REASONING_CHAT_PARAM = os.environ.get("AWM_REASONING_CHAT_PARAM", "reasoning_effort").strip()
REASONING_RESPONSES_PARAM = os.environ.get("AWM_REASONING_RESPONSES_PARAM", "reasoning").strip()
REASONING_FAIL_IF_STRIPPED = os.environ.get("AWM_REASONING_FAIL_IF_STRIPPED", "1").lower() in ("1", "true", "yes", "on")
_REASONING_FIELD_ROOTS = {"reasoning", "reasoning_effort", "reasoningEffort",
                          "chat_template_kwargs"}


def _pick_model(req: dict) -> str:
    m = (req or {}).get("model")
    return OPENAI_MODEL if (PIN_MODEL or not m) else m


# Models needing request sanitization; other OpenAI-compatible servers get bodies verbatim.
_STRICT_GPT5_PREFIXES = ("gpt-5", "claude", "o3", "o4", "azure/")


def _is_strict_gpt5_family(model: str | None) -> bool:
    if not model:
        return False
    m = str(model).lower()
    return any(m.startswith(p) for p in _STRICT_GPT5_PREFIXES)


def _reasoning_payload() -> str:
    return {"off": "none"}.get(REASONING_EFFORT, REASONING_EFFORT)


def _set_reasoning_param(body: dict, param: str, effort: str) -> None:
    key = (param or "").strip()
    if not key or key.lower() in ("none", "off", "false"):
        return
    if key in ("reasoning", "reasoning.effort"):
        body["reasoning"] = {"effort": effort}
    elif key == "reasoning_effort":
        body["reasoning_effort"] = effort
    elif key == "reasoningEffort":
        body["reasoningEffort"] = effort
    elif key in ("chat_template_kwargs.reasoning_effort",
                 "chat_template_kwargs.deepseek_reasoning_effort"):
        ctk = body.setdefault("chat_template_kwargs", {})
        if not isinstance(ctk, dict):
            ctk = {}
            body["chat_template_kwargs"] = ctk
        if effort in ("none", "off", "false", "0", "no"):
            ctk["thinking"] = False
            ctk.pop("reasoning_effort", None)
        else:
            ctk["thinking"] = True
            ctk["reasoning_effort"] = effort
    elif key in ("chat_template_kwargs.enable_thinking",
                 "chat_template_kwargs.qwen_enable_thinking"):
        ctk = body.setdefault("chat_template_kwargs", {})
        if not isinstance(ctk, dict):
            ctk = {}
            body["chat_template_kwargs"] = ctk
        ctk["enable_thinking"] = effort not in ("none", "off", "false", "0", "no")
    else:
        body[key] = effort


def _inject_reasoning_chat(body: dict) -> dict:
    effort = _reasoning_payload()
    if effort and isinstance(body, dict):
        _set_reasoning_param(body, REASONING_CHAT_PARAM, effort)
    return body


def _inject_reasoning_responses(body: dict) -> dict:
    effort = _reasoning_payload()
    if effort and isinstance(body, dict):
        _set_reasoning_param(body, REASONING_RESPONSES_PARAM, effort)
    return body


def _explicit_reasoning_field(field: str | None) -> bool:
    if not REASONING_EFFORT or not REASONING_FAIL_IF_STRIPPED or not field:
        return False
    return str(field).split(".", 1)[0] in _REASONING_FIELD_ROOTS


def _reasoning_strip_error(e: urllib.error.HTTPError, body: str,
                           field: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        e.url, e.code, e.reason, e.headers,
        io.BytesIO(
            (body + "\n[WildAWM] explicit reasoning_effort "
             f"would be stripped as unsupported field {field!r}; "
             "failing instead of silently degrading.").encode("utf-8")))


def _chat_prefers_max_tokens() -> bool:
    if CHAT_TOKEN_LIMIT_PARAM:
        return CHAT_TOKEN_LIMIT_PARAM == "max_tokens"
    return "generativelanguage.googleapis.com" in OPENAI_BASE_URL.lower()


def _degrade_images_for_text_model(messages, model: str | None) -> int:
    """Replace image parts with a vision-helper note for text-only models."""
    if not MODEL_TEXT_ONLY or not isinstance(messages, list):
        return 0

    replaced = 0
    helper = (
        "[Image payload omitted because the selected model is text-only. "
        "To inspect an image file, run `python3 "
        "/agent-config/describe_image_mcp.py <image_path> "
        "\"focused question\"` and use its returned description.]"
    )
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        normalized = []
        for part in message["content"]:
            if isinstance(part, dict) and part.get("type") in {
                "image", "image_url", "input_image",
            }:
                normalized.append({"type": "text", "text": helper})
                replaced += 1
            else:
                normalized.append(part)
        message["content"] = normalized
    return replaced


def _anthropic_model(req: dict) -> str:
    """Upstream model for /v1/messages: always OPENAI_MODEL (the Claude CLI sends its own alias)."""
    return OPENAI_MODEL

# Some models are Responses-API only: on that 400 switch to /v1/responses (or force via env).
UPSTREAM_API = os.environ.get("AWM_PROXY_UPSTREAM_API", "").strip().lower()
_USE_RESPONSES = UPSTREAM_API == "responses"


def _enable_responses_mode() -> None:
    global _USE_RESPONSES
    _USE_RESPONSES = True


def _looks_responses_only(code: int, body: str) -> bool:
    b = (body or "").lower()
    return code == 400 and "/chat/completions" in b and (
        "not accessible" in b or "responses" in b)


def _unknown_name_field(body: str) -> str | None:
    """Extract the field name from an 'Unknown name' 400 error body."""
    if not body:
        return None
    try:
        msg = (json.loads(body).get("error") or {}).get("message") or ""
        m = re.search(r'Unknown name "([^"]+)"', msg)
        if m:
            return m.group(1)
    except Exception:
        pass
    m = re.search(r'Unknown name \\?"([^"\\]+)\\?"', body)
    return m.group(1) if m else None


def _reject_json_constant(_tok):
    """json.loads parse_constant hook that rejects NaN/Infinity/-Infinity."""
    raise ValueError(f"non-RFC JSON constant on the wire: {_tok}")


def _finite_leaf(x):
    if isinstance(x, float):
        return x if (x == x and x not in (float("inf"), float("-inf"))) else None
    return x


def _coerce_text_field(x) -> str:
    """_coerce_text for a block's text field, mapping empty containers to ''."""
    if isinstance(x, (list, dict)) and not x:
        return ""
    return _coerce_text(x)


def _finite_sanitize(o):
    """Iteratively map non-finite floats in o to None (non-finite float keys to a string)."""
    if isinstance(o, dict):
        root: dict = {}
    elif isinstance(o, (list, tuple)):
        root: list = []
    else:
        return _finite_leaf(o)
    stack = [(o, root)]
    while stack:
        src, dst = stack.pop()
        items = src.items() if isinstance(src, dict) else enumerate(src)
        for k, v in items:
            if isinstance(dst, dict):
                # Non-finite float keys become a string; other keys are left to json.dumps.
                kk = "null" if (isinstance(k, float) and _finite_leaf(k) is None) else k
            else:
                kk = None
            if isinstance(v, dict):
                nd: dict = {}
                if isinstance(dst, dict):
                    dst[kk] = nd
                else:
                    dst.append(nd)
                stack.append((v, nd))
            elif isinstance(v, (list, tuple)):
                nl: list = []
                if isinstance(dst, dict):
                    dst[kk] = nl
                else:
                    dst.append(nl)
                stack.append((v, nl))
            else:
                if isinstance(dst, dict):
                    dst[kk] = _finite_leaf(v)
                else:
                    dst.append(_finite_leaf(v))
    return root


def _json_dumps_safe(obj, **kw):
    """json.dumps that never emits NaN/Infinity tokens and never raises."""
    kw.setdefault("allow_nan", False)
    kw.setdefault("default", str)
    try:
        return json.dumps(obj, **kw)
    except (ValueError, TypeError, RecursionError):
        try:
            return json.dumps(_finite_sanitize(obj), **kw)
        except Exception:
            # Last resort: never raise; degrade to a JSON string.
            try:
                return json.dumps(repr(obj)[:200000], **kw)
            except Exception:
                return '""'


def _coerce_text(x) -> str:
    """Coerce any content value to a wire-safe string (None to '', containers to JSON)."""
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, bool) or isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        # Non-finite floats have no JSON text; emit ''.
        return str(x) if (x == x and x not in (float("inf"), float("-inf"))) else ""
    def _finite(o):                       # recursively map non-finite floats -> None
        if isinstance(o, float):
            return o if (o == o and o not in (float("inf"), float("-inf"))) else None
        if isinstance(o, dict):
            return {k: _finite(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [_finite(v) for v in o]
        return o
    try:
        # allow_nan=False: strict upstreams reject NaN/Infinity tokens.
        return json.dumps(x, ensure_ascii=False, default=str, allow_nan=False)
    except Exception:
        # Only non-finite floats raise above: sanitize them to null and re-dump.
        try:
            return json.dumps(_finite(x), ensure_ascii=False, default=str, allow_nan=False)
        except Exception:
            return ""


def _tool_args_json(x) -> str:
    """Coerce a tool-call argument value to a string that always parses as JSON."""
    if x is None:
        return "{}"
    if isinstance(x, str):
        try:
            # Reject NaN/Infinity tokens that plain json.loads accepts; such strings are re-dumped below.
            json.loads(x, parse_constant=_reject_json_constant)
            return x
        except Exception:
            try:
                return json.dumps(x)      # raw string -> JSON string literal
            except Exception:
                return "{}"
    try:
        # Raise on non-finite floats rather than emit invalid JSON.
        return json.dumps(x, ensure_ascii=False, default=str, allow_nan=False)
    except Exception:
        # Sanitize non-finite leaves to null, keeping all other keys and values.
        try:
            return json.dumps(_finite_sanitize(x), ensure_ascii=False,
                              default=str, allow_nan=False)
        except Exception:
            return "{}"


def _tool_def_fields(raw_name, raw_description, raw_parameters) -> dict:
    """Normalize a tool definition's name, description and parameters for strict upstreams."""
    params = raw_parameters if isinstance(raw_parameters, dict) else {"type": "object"}
    # Tool schemas may carry non-finite floats too; sanitize them.
    params = _finite_sanitize(params)
    return {
        "name": _coerce_text(raw_name) or "tool",
        "description": _coerce_text(raw_description),
        "parameters": params,
    }


def _is_image_block(b) -> bool:
    """A content block that represents a (renderable) image, in either dialect."""
    return isinstance(b, dict) and b.get("type") in (
        "image", "image_url", "input_image")


def _flatten_block_text(blocks) -> str:
    """Join the text of a content-block list, skipping images at any depth (iterative)."""
    if not isinstance(blocks, list):
        blocks = [blocks]
    parts = []
    # LIFO stack; push children in REVERSE so they pop in document order.
    stack = list(reversed(blocks))
    while stack:
        b = stack.pop()
        if _is_image_block(b):
            continue                           # rendered separately, not text
        if isinstance(b, dict):
            if "text" in b:
                parts.append(_coerce_text_field(b.get("text")))
            # other dict block types (e.g. tool_use) carry no flat text here
        elif isinstance(b, list):
            stack.extend(reversed(b))          # sub-array: descend (images skipped)
        else:
            parts.append(_coerce_text(b))
    # Newline-join so block boundaries survive; drop empty pieces.
    return "\n".join(p for p in parts if p)


def _iter_render_images(node, kind: str):
    """Yield renderable images found anywhere under node, in the shape selected by kind."""
    def _uri_from(cur):
        # Returns (uri or None, stop_descent); a malformed node yields no image instead of raising.
        t = cur.get("type")
        if t == "image":                                   # anthropic block
            src = cur.get("source")
            src = src if isinstance(src, dict) else {}
            if src.get("type") == "base64" and src.get("data"):
                # Sanitize media_type so it cannot inject a second ';base64,' marker.
                uri = "data:%s;base64,%s" % (
                    _sanitize_media_type(src.get("media_type")), src["data"])
                # Stop descending only if the image is saveable, mirroring the capture walk.
                return (uri, _data_uri_hash(uri) is not None)
            # URL-source image: forward it but keep descending (capture does not stop here).
            if src.get("type") == "url" and isinstance(src.get("url"), str) \
                    and src["url"].strip():
                return (src["url"].strip(), False)
            return (None, False)
        if t in ("image_url", "input_image"):              # openai chat/responses
            iu = cur.get("image_url", "")
            u = iu if isinstance(iu, str) else (
                iu.get("url", "") if isinstance(iu, dict) else "")
            if isinstance(u, str) and u.strip():
                u = u.strip()
                # Same rule: stop only on a saveable data: URI; URLs and unsaveable wrappers descend.
                return (u, u.lower().startswith("data:") and _data_uri_hash(u) is not None)
            return (None, False)
        return (None, False)

    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            # Skip a malformed node rather than raising out of the generator.
            try:
                uri, stop_descent = _uri_from(cur)
            except Exception as e:
                sys.stderr.write(f"[proxy] render walk: skipping malformed node: {e}\n")
                uri, stop_descent = None, False
            if uri is not None:
                if kind == "data_uri":
                    # Byte-hashing callers want data: URIs only, not URL references.
                    if uri.lower().startswith("data:"):
                        yield uri
                elif kind == "image_url":
                    yield {"type": "image_url", "image_url": {"url": uri}}
                elif kind == "input_image":
                    yield {"type": "input_image", "image_url": uri}
                if stop_descent:
                    continue
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def _step_images_for_tail(tail) -> list:
    """Filenames of saved images present in this step's tail, looked up by hash (no re-save)."""
    out: list = []
    seen: set = set()

    def _emit(uri):
        h = _data_uri_hash(uri)
        if not h:
            return
        with _CALL_LOCK:
            fn = _IMG_SEEN.get(h)
        if fn and fn not in seen:
            seen.add(fn)
            out.append(fn)
    try:
        # (a) images renderable directly in the tail
        for uri in _iter_render_images({"_": tail}, "data_uri"):
            _emit(uri)
        # (b) images inside tool-call arguments, attributed to the step that first sends the call.
        for m in (tail or []):
            if not isinstance(m, dict):
                continue
            for tc in (m.get("tool_calls") or []):
                a = ((tc or {}).get("function") or {}).get("arguments")
                if not isinstance(a, str):
                    continue
                try:
                    parsed = json.loads(a)
                except Exception:
                    continue
                for uri in _iter_render_images(parsed, "data_uri"):
                    _emit(uri)
    except Exception as e:                                        # never break a call
        sys.stderr.write(f"[proxy] step-image map error: {e}\n")
    return out


def _data_uri_hash(uri: str):
    """sha256 of a data: URI's bytes, or None if _save_image would not save it."""
    try:
        if not isinstance(uri, str):
            return None
        uri = uri.strip()   # strip like the forward walk so hashes match
        if uri[:5].lower() != "data:":
            return None
        # Split like _save_data_uri so a comma in media_type doesn't change the hash.
        b64 = uri.split(";base64,", 1)[1] if ";base64," in uri else uri.split(",", 1)[1]
        raw = _b64.b64decode(b64)
    except Exception:
        return None
    if len(raw) < 64:
        return None
    return _hash.sha256(raw).hexdigest()


def _strip_tool_args(o):
    """Deep copy of o with tool-call arguments blanked, leaving only renderable images."""
    if isinstance(o, dict):
        t = o.get("type")
        return {k: ("" if ((t == "tool_use" and k == "input")
                           or (t == "function_call" and k == "arguments"))
                    else _strip_tool_args(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_strip_tool_args(v) for v in o]
    return o


def _args_strings_image_hashes(args_strings) -> set:
    """Content hashes of images embedded in tool-call arguments JSON strings."""
    out = set()
    for a in args_strings:
        if not isinstance(a, str):
            continue
        try:
            parsed = json.loads(a)
        except Exception:
            continue
        # Reuse the render walk so every forwarded image shape is recognized.
        try:
            for uri in _iter_render_images(parsed, "data_uri"):
                h = _data_uri_hash(uri)
                if h:
                    out.add(h)
        except Exception:
            pass
    return out


def _argscall_image_hashes(messages: list) -> set:
    """Args-channel image hashes of a chat body's tool_call arguments."""
    if not isinstance(messages, list):
        return set()
    return _args_strings_image_hashes(
        ((tc or {}).get("function") or {}).get("arguments")
        for m in messages if isinstance(m, dict)
        for tc in (m.get("tool_calls") or []))


def _renderable_content_hashes(src_req: dict) -> set:
    """Hashes of images at renderable positions in src_req (tool-call args excluded)."""
    out = set()
    for key in ("messages", "input", "system"):
        try:
            for uri in _iter_render_images(_strip_tool_args((src_req or {}).get(key)), "data_uri"):
                h = _data_uri_hash(uri)
                if h:
                    out.add(h)
        except Exception:
            pass
    return out


def _ensure_images_forwarded(src_req: dict, messages: list) -> None:
    """Append any saved-but-unforwarded image to the built chat messages; never raises."""
    try:
        if not isinstance(messages, list):
            return
        # images already forwarded as renderable parts in the built body
        forwarded = set()
        for uri in _iter_render_images(messages, "data_uri"):
            h = _data_uri_hash(uri)
            if h:
                forwarded.add(h)
        # Images saved for this request (same containers as _scan_images, incl. system).
        missing = {}   # hash -> data_uri (first occurrence, document order)
        for key in ("messages", "input", "system"):
            for uri in _iter_render_images((src_req or {}).get(key), "data_uri"):
                h = _data_uri_hash(uri)
                if h and h not in forwarded and h not in missing:
                    missing[h] = uri
        # Images only inside tool-call args already reach the model; don't re-inject them.
        if missing:
            content_h = _renderable_content_hashes(src_req)
            # Match by decoded-content hash; escaped newlines in arguments defeat substring matching.
            args_h = _argscall_image_hashes(messages)
            for _h in list(missing):
                if _h in content_h:
                    continue   # has a renderable home the converter dropped -> re-inject
                if _h in args_h:
                    del missing[_h]   # args-ONLY image (reached the model as args bytes)
        if not missing:
            return
        parts = [{"type": "image_url", "image_url": {"url": u}}
                 for u in missing.values()]
        # Attach to the nearest user message, never between tool_calls and their tool results.
        target = None
        for m in reversed(messages):
            if not isinstance(m, dict):
                continue
            if m.get("role") == "user":
                target = m
                break
            if m.get("role") == "tool" or (
                    m.get("role") == "assistant" and m.get("tool_calls")):
                # Skip a trailing tool_calls/tool run.
                continue
            # Other non-user tail: fall back to a fresh user message.
            break
        if target is not None:
            c = target.get("content")
            if isinstance(c, list):
                target["content"] = c + parts
            elif isinstance(c, str) and c:
                target["content"] = [{"type": "text", "text": c}] + parts
            else:
                target["content"] = parts
        else:
            # No user message: insert before the trailing tool/tool_calls run to keep pairing.
            ins = len(messages)
            while ins > 0:
                pm = messages[ins - 1]
                if isinstance(pm, dict) and (
                        pm.get("role") == "tool"
                        or (pm.get("role") == "assistant" and pm.get("tool_calls"))):
                    ins -= 1
                    continue
                break
            messages.insert(ins, {"role": "user", "content": parts})
    except Exception as e:                                        # never break a call
        sys.stderr.write(f"[proxy] image-forward safety net error: {e}\n")


def _forward_responses_input_images(src_req: dict, input_items: list) -> None:
    """Append saved-but-unforwarded images to a /responses input list; never raises."""
    try:
        if not isinstance(input_items, list):
            return
        # images already present as renderable parts in the built /responses input
        forwarded = set()
        for uri in _iter_render_images(input_items, "data_uri"):
            h = _data_uri_hash(uri)
            if h:
                forwarded.add(h)
        # images SAVED for this request (same containers _scan_images walks)
        missing = {}   # hash -> data_uri (first occurrence, document order)
        for key in ("messages", "input", "system"):
            for uri in _iter_render_images((src_req or {}).get(key), "data_uri"):
                h = _data_uri_hash(uri)
                if h and h not in forwarded and h not in missing:
                    missing[h] = uri
        # Skip images that only appear in function_call arguments (they reach the model as args).
        if missing:
            content_h = _renderable_content_hashes(src_req)
            # Match by decoded-content hash (whitespace-immune).
            args_h = _args_strings_image_hashes(
                it.get("arguments") for it in input_items
                if isinstance(it, dict) and it.get("type") == "function_call")
            for _h in list(missing):
                if _h in content_h:
                    continue   # renderable home the converter dropped -> re-inject
                if _h in args_h:
                    del missing[_h]
        if not missing:
            return
        parts = [{"type": "input_image", "image_url": u} for u in missing.values()]
        # Attach to the nearest user input, skipping a trailing function_call/output run.
        target = None
        for it in reversed(input_items):
            if not isinstance(it, dict):
                continue
            if it.get("type") == "message" and it.get("role") == "user":
                target = it
                break
            if it.get("type") in ("function_call", "function_call_output"):
                continue
            break
        if target is not None and isinstance(target.get("content"), list):
            target["content"] = list(target["content"]) + parts
        elif target is not None:
            # Non-list user content: coerce to parts and merge the images in.
            tc = target.get("content")
            base_parts = ([{"type": "input_text", "text": tc}]
                          if isinstance(tc, str) and tc else [])
            target["content"] = base_parts + parts
        else:
            # No user item: insert before the trailing function_call run to keep pairing.
            ins = len(input_items)
            while ins > 0:
                pit = input_items[ins - 1]
                if isinstance(pit, dict) and pit.get("type") in (
                        "function_call", "function_call_output"):
                    ins -= 1
                    continue
                break
            input_items.insert(ins, {"type": "message", "role": "user",
                                     "content": parts})
    except Exception as e:                                        # never break a call
        sys.stderr.write(f"[proxy] /responses image-forward net error: {e}\n")


def _strip_cache_control(messages):
    """Remove cache_control fields from Anthropic content blocks."""
    out = []
    for m in messages:
        if isinstance(m.get("content"), list):
            new_content = []
            for block in m["content"]:
                if isinstance(block, dict):
                    block = {k: v for k, v in block.items()
                             if k not in ("cache_control",)}
                new_content.append(block)
            m = {**m, "content": new_content}
        out.append(m)
    return out


def _repair_tool_pairing(messages: list) -> list:
    """Fix tool_call/tool pairing for strict backends; returns a new list, never raises."""
    try:
        if not isinstance(messages, list):
            return messages
        # Pass 1: coalesce split parallel batches (same as the sig-normalizer).
        messages = _coalesce_assistant_tool_calls(messages)
        # Pass 2: tool_calls only on an assistant message; strip from anything else.
        pre = []
        for m in messages:
            if isinstance(m, dict) and m.get("tool_calls") \
                    and m.get("role") != "assistant":
                m = {k: v for k, v in m.items() if k != "tool_calls"}
            pre.append(m)

        def _is_asst_tc(x):
            return (isinstance(x, dict) and x.get("role") == "assistant"
                    and x.get("tool_calls"))

        # Pass 3: move each tool result after its offering assistant; ids coerced to hashable text.
        asst_offers = {}        # assistant index -> offered tool_call ids
        for idx, m in enumerate(pre):
            if _is_asst_tc(m):
                asst_offers[idx] = {_coerce_text(tc.get("id")) for tc in m["tool_calls"]
                                    if isinstance(tc, dict)}
        owner_of = {}           # tool index -> owning assistant index (None = orphan)
        attached = {}           # assistant index -> its tool indices, in order
        for idx, m in enumerate(pre):
            if isinstance(m, dict) and m.get("role") == "tool":
                tcid = _coerce_text(m.get("tool_call_id"))
                owner = None
                for a in range(idx - 1, -1, -1):          # nearest preceding offerer
                    if a in asst_offers and tcid in asst_offers[a]:
                        owner = a
                        break
                if owner is None:
                    for a in range(idx + 1, len(pre)):    # else nearest following
                        if a in asst_offers and tcid in asst_offers[a]:
                            owner = a
                            break
                owner_of[idx] = owner
                if owner is not None:
                    attached.setdefault(owner, []).append(idx)
        norm = []
        for idx, m in enumerate(pre):
            if isinstance(m, dict) and m.get("role") == "tool" \
                    and owner_of.get(idx) is not None:
                continue                                  # emitted right after owner
            norm.append(m)
            if idx in attached:
                for tidx in attached[idx]:
                    norm.append(pre[tidx])

        # Pass 4: demote a tool message not directly preceded by its offering assistant to user.
        out = []
        for i, m in enumerate(norm):
            if isinstance(m, dict) and m.get("role") == "tool":
                tcid = _coerce_text(m.get("tool_call_id"))
                is_attached = False
                j = i - 1
                while j >= 0 and isinstance(norm[j], dict) \
                        and norm[j].get("role") == "tool":
                    j -= 1                                # skip sibling tool results
                if j >= 0 and _is_asst_tc(norm[j]):
                    offered_here = {_coerce_text(tc.get("id")) for tc in norm[j]["tool_calls"]
                                    if isinstance(tc, dict)}
                    is_attached = tcid in offered_here
                if is_attached:
                    out.append(m)
                else:
                    out.append({"role": "user",
                                "content": _coerce_text(m.get("content"))})
            else:
                out.append(m)

        # Pass 5: drop unanswered offered calls unless they trail the conversation (pending call).
        for i, m in enumerate(out):
            if not _is_asst_tc(m):
                continue
            answered = set()
            k = i + 1
            while k < len(out) and isinstance(out[k], dict) \
                    and out[k].get("role") == "tool":
                answered.add(_coerce_text(out[k].get("tool_call_id")))
                k += 1
            trailing = (k >= len(out))     # nothing non-tool follows: a pending call
            kept = []
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                if _coerce_text(tc.get("id")) in answered or trailing:
                    kept.append(tc)
                # An unanswered call followed by a non-tool message can never pair; drop it.
            if len(kept) == len(m["tool_calls"]):
                continue
            nm = dict(m)
            if kept:
                nm["tool_calls"] = kept
            else:
                nm.pop("tool_calls", None)
            out[i] = nm
        return out
    except Exception as e:                                        # never break a call
        sys.stderr.write(f"[proxy] tool-pairing repair error: {e}\n")
        return messages


def _collect_tool_ids(node, out: set) -> None:
    """Collect every non-empty tool-call/tool-result id under node (iteratively) into out."""
    stack = [node]
    keys = ("id", "tool_use_id", "tool_call_id", "call_id")
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            for k in keys:
                v = n.get(k)
                if isinstance(v, str) and v:
                    out.add(v)
            for v in n.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(n, list):
            stack.extend(n)


def _make_unique_id_minter(existing: set):
    """Return a zero-arg minter of 'call_<n>' ids that never collide with existing ones."""
    used = set(existing)
    seq = [0]

    def _mint() -> str:
        while True:
            seq[0] += 1
            cand = "call_%d" % seq[0]
            if cand not in used:
                used.add(cand)
                return cand

    return _mint


# A dict whose type is one of these is a content block; other dicts are structured values.
_CONTENT_BLOCK_TYPES = {
    "text", "input_text", "output_text",
    "image", "image_url", "input_image",
    "tool_use", "tool_result", "function_call", "function_call_output",
    "document", "thinking", "redacted_thinking", "reasoning",
}


def _is_content_block_dict(d) -> bool:
    return isinstance(d, dict) and d.get("type") in _CONTENT_BLOCK_TYPES


def _blank_image_data(o):
    """Deep copy of o with image-source data and data: URLs blanked."""
    if isinstance(o, dict):
        is_img_src = (o.get("type") == "base64")
        out = {}
        for k, v in o.items():
            if k == "data" and isinstance(v, str) and is_img_src:
                out[k] = ""
            elif k == "url" and isinstance(v, str) and v[:5].lower() == "data:":
                out[k] = ""
            else:
                out[k] = _blank_image_data(v)
        return out
    if isinstance(o, (list, tuple)):
        return [_blank_image_data(v) for v in o]
    return o


def _content_text_safe(content) -> str:
    """Flatten content/system to text without ever leaking raw image base64."""
    if isinstance(content, (list, dict)) and not content:
        return ""   # empty container -> no text (avoid a literal '{}' block)
    if isinstance(content, list):
        return _flatten_block_text(content)
    if isinstance(content, dict):
        if content.get("type") in _CONTENT_BLOCK_TYPES:
            return _flatten_block_text(content)
        return _coerce_text(_blank_image_data(content))
    return _coerce_text(content)


# Hidden chain-of-thought block types; never surfaced in visible text or the trajectory.
_HIDDEN_COT_BLOCK_TYPES = {
    "reasoning", "reasoning_text", "reasoning_summary",
    "summary_text", "thinking", "redacted_thinking",
}
_VISIBLE_TEXT_BLOCK_TYPES = {"text", "output_text", "input_text"}

# Claude Code treats an empty end_turn as an error; emit a small visible block instead.
_EMPTY_TURN_VISIBLE_FALLBACK = (
    "The model completed this turn without emitting a visible response."
)


def _visible_text(content) -> str:
    """Visible text of a response content value, with hidden chain-of-thought blocks stripped."""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, dict)) and not content:
        return ""
    if isinstance(content, dict):
        if content.get("type") in _HIDDEN_COT_BLOCK_TYPES:
            return ""
        return _content_text_safe(content)
    if not isinstance(content, list):
        return _content_text_safe(content)
    parts = []
    stack = list(reversed(content))
    while stack:
        b = stack.pop()
        if isinstance(b, dict):
            bt = b.get("type")
            if bt in _HIDDEN_COT_BLOCK_TYPES:
                continue                          # hidden CoT -> never surfaced
            if _is_image_block(b):
                continue                          # rendered elsewhere, not text
            if "text" in b:
                parts.append(_coerce_text_field(b.get("text")))
        elif isinstance(b, list):
            stack.extend(reversed(b))
        else:
            parts.append(_coerce_text(b))
    return "\n".join(p for p in parts if p)


def anthropic_to_openai(req: dict) -> dict:
    """Translate Anthropic /v1/messages → OpenAI /v1/chat/completions."""
    messages = req.get("messages", [])
    system = req.get("system", "")

    out_messages = []
    if system:
        if isinstance(system, str):
            system_text = system
            system_imgs = []
        elif isinstance(system, list):
            # Flatten text; attach system images as image_url parts on the leading system message.
            system_text = _flatten_block_text(system)
            system_imgs = list(_iter_render_images(system, "image_url"))
        else:
            # Non-list system: text only, images stripped (re-injected on a user message later).
            system_text = _content_text_safe(system)
            system_imgs = []
        if system_text or system_imgs:
            if system_imgs:
                content = ([{"type": "text", "text": system_text}]
                           if system_text else []) + system_imgs
            else:
                content = system_text
            out_messages.append({"role": "system", "content": content})

    # Seed the placeholder minter past all existing ids so minted ids never collide.
    _existing_ids: set = set()
    _collect_tool_ids(messages, _existing_ids)
    _next_tool_id = _make_unique_id_minter(_existing_ids)
    # Placeholder ids minted for null-id tool_use blocks, paired FIFO with null-id tool_results.
    _null_id_q: list = []

    def _tool_use_id(raw) -> str:
        """Wire id for a tool_use; unusable ids get a minted placeholder queued for pairing."""
        s = _usable_id_str(raw)
        if s:
            return s
        pid = _next_tool_id()
        _null_id_q.append(pid)
        return pid

    def _tool_result_id(raw) -> str:
        """tool_call_id for a tool_result; unusable ids pair FIFO with a null-id placeholder."""
        s = _usable_id_str(raw)
        if s:
            return s
        return _null_id_q.pop(0) if _null_id_q else _next_tool_id()

    for m in _strip_cache_control(messages):
        role = m.get("role", "user")
        content = m.get("content", "")
        if _is_content_block_dict(content):
            # Wrap a bare content-block dict so it takes the list path; other dicts keep JSON.
            content = [content]
        if isinstance(content, list):
            text_parts = []
            tool_calls = []
            tool_results = []
            image_msgs = []
            top_image_parts = []   # image_url parts for top-level images
            for block in content:
                if not isinstance(block, dict):
                    # Nested sub-array: forward any images it holds as top-level image_url parts.
                    top_image_parts.extend(
                        _iter_render_images(block, "image_url"))
                    # Also recover nested text so it is not dropped.
                    nested_text = _flatten_block_text(block)
                    if nested_text:
                        text_parts.append(nested_text)
                    continue
                t = block.get("type")
                if t == "text":
                    text_parts.append(_coerce_text_field(block.get("text")))
                elif t == "image":
                    # Top-level user image: re-emit as image_url; a malformed source yields none.
                    src = block.get("source")
                    src = src if isinstance(src, dict) else {}
                    if src.get("type") == "base64" and src.get("data"):
                        top_image_parts.append({
                            "type": "image_url",
                            # Sanitize media_type so it cannot inject into the data: URI.
                            "image_url": {"url": "data:%s;base64,%s" % (
                                _sanitize_media_type(src.get("media_type")),
                                src["data"])},
                        })
                    elif src.get("type") == "url" and isinstance(src.get("url"), str) \
                            and src["url"].strip():
                        # URL-source image: forward the URL verbatim.
                        top_image_parts.append({
                            "type": "image_url",
                            "image_url": {"url": src["url"].strip()},
                        })
                elif t == "tool_use":
                    # tool_use is only legal on assistant turns; drop it elsewhere.
                    if role != "assistant":
                        continue
                    # One tool_calls array per turn; results must follow immediately.
                    tool_calls.append({
                        "id": _tool_use_id(block.get("id")),
                        "type": "function",
                        "function": {
                            "name": _coerce_text(block.get("name")) or "tool",
                            "arguments": _tool_args_json(block.get("input")),
                        },
                    })
                elif t == "tool_result":
                    c = block.get("content")
                    # Tool messages cannot carry images; re-emit them as a user message after.
                    if isinstance(c, (list, dict)):
                        images = list(_iter_render_images(c, "data_uri"))
                        c = _content_text_safe(c)
                    else:
                        images = []
                        c = _coerce_text(c)
                    # tool_result is user-only; elsewhere fold it in, no orphan tool message.
                    if role == "user":
                        # Resolve the id once (pops the null-id FIFO); reuse it for the image label.
                        _trid = _tool_result_id(block.get("tool_use_id"))
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": _trid,
                            "content": c,
                        })
                        if images:
                            image_msgs.append({
                                "role": "user",
                                "content": [{
                                    "type": "text",
                                    "text": "[image returned by tool call "
                                            f"{_trid}]",
                                }] + [{
                                    "type": "image_url",
                                    "image_url": {"url": u},
                                } for u in images],
                            })
                    else:
                        if c:
                            text_parts.append(c)
                        top_image_parts.extend(
                            {"type": "image_url", "image_url": {"url": u}}
                            for u in images)
                else:
                    # Unknown/typeless dict block: forward nested images and recover its flat text.
                    top_image_parts.extend(
                        _iter_render_images(block, "image_url"))
                    nested_text = _flatten_block_text(block)
                    if nested_text:
                        text_parts.append(nested_text)
            joined_text = "\n".join(p for p in text_parts if p)
            if tool_calls:
                # Attach images here so nothing wedges between tool_calls and results.
                if top_image_parts:
                    a_content = ([{"type": "text", "text": joined_text}]
                                 if joined_text else []) + top_image_parts
                    top_image_parts = []   # consumed above
                else:
                    a_content = joined_text
                out_messages.append({
                    "role": "assistant",
                    "content": a_content,
                    "tool_calls": tool_calls,
                })
                joined_text = ""
            # Tool results directly follow tool_calls; sibling text and images go after.
            out_messages.extend(tool_results)
            out_messages.extend(image_msgs)
            if top_image_parts:
                # Top-level image without tool_calls: send multimodal content.
                parts = ([{"type": "text", "text": joined_text}]
                         if joined_text else []) + top_image_parts
                out_messages.append({"role": role, "content": parts})
                joined_text = ""
            elif joined_text or not (tool_calls or tool_results or image_msgs):
                out_messages.append({"role": role, "content": joined_text})
        else:
            # Null -> ''; other values coerced to text with image bytes stripped.
            out_messages.append({
                "role": role,
                "content": _content_text_safe(content),
            })

    # Allow one leading system message: coalesce leading ones; relabel later ones as user in place.
    _fixed = []
    _seen_non_system = False
    _lead_sys_text = []
    _lead_sys_imgs = []
    _deferred_relabel = []   # relabeled system messages deferred past tool runs

    def _emit_lead_system():
        """Emit one leading system message (multimodal if it has images), or nothing if empty."""
        if not (any(p for p in _lead_sys_text) or _lead_sys_imgs):
            return
        text = "\n".join(p for p in _lead_sys_text if p)
        if _lead_sys_imgs:
            content = ([{"type": "text", "text": text}] if text else []) \
                + list(_lead_sys_imgs)
        else:
            content = text
        _fixed.append({"role": "system", "content": content})

    def _flush_deferred():
        for d in _deferred_relabel:
            _fixed.append(d)
        _deferred_relabel.clear()

    for m in out_messages:
        if m.get("role") == "system":
            c = m.get("content", "")
            if isinstance(c, list):
                stext = _flatten_block_text(c)
                simgs = list(_iter_render_images(c, "image_url"))
            else:
                stext = _coerce_text(c)
                simgs = []
            if _seen_non_system:
                # Relabel to user, deferred past any following tool messages to keep adjacency.
                if simgs:
                    ucontent = ([{"type": "text", "text": stext}] if stext else []) \
                        + simgs
                else:
                    ucontent = stext
                _deferred_relabel.append({"role": "user", "content": ucontent})
            else:
                _lead_sys_text.append(stext)
                _lead_sys_imgs.extend(simgs)
        else:
            if not _seen_non_system:
                _emit_lead_system()
                _seen_non_system = True
            if m.get("role") == "tool":
                _fixed.append(m)
            else:
                _flush_deferred()   # safe boundary for deferred relabels
                _fixed.append(m)
    if not _seen_non_system:
        _emit_lead_system()
    _flush_deferred()
    out_messages = _fixed

    # Final backstop: guarantee valid tool_calls/tool adjacency.
    out_messages = _repair_tool_pairing(out_messages)

    openai_req = {
        # Claude Code sends a fixed model alias; always map to the configured model.
        "model": _anthropic_model(req),
        "messages": out_messages,
    }

    # Omit empty tools: strict upstreams reject tools: [].
    if req.get("tools"):
        openai_req["tools"] = [
            {
                "type": "function",
                # Coerce bad tool definitions instead of failing the whole turn.
                "function": _tool_def_fields(
                    t.get("name"), t.get("description"),
                    t.get("input_schema", {"type": "object"})),
            }
            for t in req["tools"]
        ]

    if "tool_choice" in req:
        tc = req["tool_choice"]
        if isinstance(tc, dict):
            if tc.get("type") == "auto":
                openai_req["tool_choice"] = "auto"
            elif tc.get("type") == "none":
                openai_req["tool_choice"] = "none"
            elif tc.get("type") == "any":
                openai_req["tool_choice"] = "required"
            elif tc.get("type") == "tool":
                # Name a function only when the name is non-empty; else fall back to 'required'.
                _tcn = _coerce_text(tc.get("name"))
                if _tcn:
                    openai_req["tool_choice"] = {
                        "type": "function",
                        "function": {"name": _tcn},
                    }
                else:
                    openai_req["tool_choice"] = "required"

    # Forward max_tokens: reasoning models otherwise exhaust a small default on hidden reasoning.
    if "max_tokens" in req:
        try:
            openai_req["max_tokens"] = int(req["max_tokens"])
        except (TypeError, ValueError):
            pass

    # Operator override; takes precedence over the client budget.
    _force_mct = os.environ.get("PROXY_FORCE_MAX_COMPLETION_TOKENS")
    if _force_mct:
        try:
            openai_req.pop("max_tokens", None)
            openai_req["max_completion_tokens"] = int(_force_mct)
        except (TypeError, ValueError):
            pass

    _inject_reasoning_chat(openai_req)
    return openai_req


def openai_to_anthropic(resp: dict, model: str) -> dict:
    """Translate OpenAI response → Anthropic /v1/messages response."""
    # Degenerate bodies (null choice/message) degrade to an empty assistant turn, not a 500.
    choice = (resp.get("choices") or [{}])[0]
    if not isinstance(choice, dict):
        choice = {}
    msg = choice.get("message")
    if not isinstance(msg, dict):
        msg = {}
    # Content may be a list or scalar; text must be a string with hidden CoT stripped.
    _content = msg.get("content")
    text = _visible_text(_content)
    _tcs = msg.get("tool_calls")
    tool_calls = _tcs if isinstance(_tcs, list) else []
    if not text and not tool_calls:
        text = _EMPTY_TURN_VISIBLE_FALLBACK
    # Mint collision-proof ids for tool_calls lacking a usable string id.
    _tc_ids = {tc.get("id") for tc in tool_calls
               if isinstance(tc, dict) and isinstance(tc.get("id"), str) and tc.get("id")}
    _mint_tcid = _make_unique_id_minter(_tc_ids)

    content = []
    if text:
        content.append({"type": "text", "text": text})
    for tc in tool_calls:
        if not _is_usable_tool_call(tc):
            continue   # skip null/empty tool_call
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        # Arguments should be a JSON string but may be a dict/list/scalar; normalize first.
        raw_args = fn.get("arguments", "")
        if not isinstance(raw_args, str):
            raw_args = _tool_args_json(raw_args)
        raw_args = raw_args or ""
        if not raw_args.strip():
            args = {}  # legitimately argument-less call
        else:
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError as e:
                # Emit a schema-invalid input so the client reports a tool error the model can fix.
                sys.stderr.write(
                    "upstream returned unparseable tool_call arguments for "
                    f"{fn.get('name')!r} (id={tc.get('id')}): {e}; "
                    f"raw={raw_args[:300]!r}\n")
                args = {
                    "_invalid_tool_arguments": raw_args,
                    "_proxy_error": f"tool arguments were not valid JSON: {e}",
                }
        content.append({
            "type": "tool_use",
            # Never emit a null id or name.
            "id": _usable_id_str(tc.get("id")) or _mint_tcid(),
            "name": _coerce_text(fn.get("name")) or _coerce_text(tc.get("name")) or "tool",
            "input": args,
        })

    stop_reason = "end_turn"
    fr = choice.get("finish_reason")
    # Some providers return finish_reason=stop with tool_calls; tool calls win.
    if tool_calls or fr == "tool_calls":
        stop_reason = "tool_use"
    elif fr == "length":
        stop_reason = "max_tokens"
    elif fr == "stop":
        stop_reason = "end_turn"

    usage = resp.get("usage")          # present-but-null / non-dict -> {}
    usage = usage if isinstance(usage, dict) else {}

    return {
        "id": resp.get("id", "msg_proxy"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _anthropic_message_sse_events(message: dict) -> list[tuple[str, dict]]:
    usage = message.get("usage") or {}
    events = [(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message.get("id", "msg_proxy"),
                "type": "message",
                "role": "assistant",
                "model": message.get("model", OPENAI_MODEL),
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": 0,
                },
            },
        },
    )]
    for index, block in enumerate(message.get("content") or []):
        if block.get("type") == "tool_use":
            events.append((
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": block.get("id", f"call_{index}"),
                        "name": block.get("name", "tool"),
                        "input": {},
                    },
                },
            ))
            events.append((
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            block.get("input") or {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                },
            ))
        else:
            text = _coerce_text(block.get("text", ""))
            events.append((
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
            ))
            if text:
                events.append((
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": text},
                    },
                ))
        events.append((
            "content_block_stop",
            {"type": "content_block_stop", "index": index},
        ))
    events.extend([
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": message.get("stop_reason") or "end_turn",
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": usage.get("output_tokens", 0)},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ])
    return events


def _chat_url() -> str:
    # Append /chat/completions; a bare resource root also gets /openai/v1.
    base = OPENAI_BASE_URL.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/openai/v1") or base.endswith("/v1") or base.endswith("/openai"):
        return base + "/chat/completions"
    return base + "/openai/v1/chat/completions"


def _responses_url() -> str:
    # Same base-URL handling as the chat URL builder.
    base = OPENAI_BASE_URL.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if base.endswith("/responses"):
        return base
    if base.endswith("/openai/v1") or base.endswith("/v1") or base.endswith("/openai"):
        return base + "/responses"
    return base + "/openai/v1/responses"


def _auth_headers() -> dict:
    # Send both api-key and Bearer auth so either provider style accepts the request.
    return {
        "Content-Type": "application/json",
        "api-key": OPENAI_API_KEY,
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }


# Gemini thought signatures must be echoed back on resend; stash by tool_call id and reinject.
_THOUGHT_SIGS = {}  # tool_call_id -> thought_signature


def _capture_tool_sigs(resp_json: dict) -> None:
    # Skip null/non-dict choices and messages instead of crashing.
    for ch in (resp_json.get("choices") or []):
        if not isinstance(ch, dict):
            continue
        msg = ch.get("message")
        if not isinstance(msg, dict):
            continue
        for tc in (msg.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            tcid = tc.get("id")
            sig = ((tc.get("extra_content") or {}).get("google") or {}).get("thought_signature")
            if isinstance(tcid, str) and tcid and sig:
                _THOUGHT_SIGS[tcid] = sig


def _merge_tool_call_deltas(deltas):
    """Merge streaming tool_call deltas into ordered call slots."""
    slots = {}        # key -> slot
    order = []        # keys in creation order
    # Last slot key per choice; scoping by choice keeps n>1 samples apart.
    last_key_by_choice = {}
    auto_seq = 0
    for item in deltas:
        # Items are (choice_index, tc) tuples or bare tc dicts; a bad choice index falls back to 0.
        if isinstance(item, tuple):
            ch_idx, tc = item
        else:
            ch_idx, tc = 0, item
        if not (isinstance(ch_idx, int) and not isinstance(ch_idx, bool)):
            ch_idx = 0
        if not isinstance(tc, dict):
            continue
        # Tolerate a non-dict function and non-string sub-fields, as the buffered path does.
        fn = tc.get("function")
        fn = fn if isinstance(fn, dict) else {}
        idx = tc.get("index")
        # Reject bool indices (bool is an int subclass) so distinct calls never merge.
        last_key = last_key_by_choice.get(ch_idx)
        if isinstance(idx, int) and not isinstance(idx, bool):
            key = ("idx", ch_idx, idx)
        else:
            cur = slots.get(last_key) if last_key is not None else None
            _name = fn.get("name")
            _name_str = _name if isinstance(_name, str) else ""
            if cur is None or tc.get("id") or (_name_str and cur["name"]):
                key = ("auto", ch_idx, auto_seq)
                auto_seq += 1
            else:
                key = last_key
        last_key_by_choice[ch_idx] = key
        slot = slots.get(key)
        if slot is None:
            slot = slots[key] = {"id": None, "name": "", "arguments": "",
                                 "sig": None}
            order.append(key)
        tcid = tc.get("id")
        if isinstance(tcid, str) and tcid:
            slot["id"] = tcid
        # Names are set once from a non-empty string, never concatenated.
        name = fn.get("name")
        if isinstance(name, str) and name and not slot["name"]:
            slot["name"] = name
        # Coerce non-string argument fragments to JSON before concatenating.
        a = fn.get("arguments")
        if a is not None and a != "":
            slot["arguments"] += a if isinstance(a, str) else _tool_args_json(a)
        _ec = tc.get("extra_content")
        _ec = _ec if isinstance(_ec, dict) else {}
        _g = _ec.get("google")
        _g = _g if isinstance(_g, dict) else {}
        sig = _g.get("thought_signature")
        if sig:
            slot["sig"] = sig
    return [slots[k] for k in order]


# SSE line terminators only; U+2028/U+2029/U+0085 inside JSON are not boundaries.
_SSE_NEWLINE_RE = re.compile(r"\r\n|\r|\n")


def _iter_sse_data_objects(sse_text: str):
    """Yield each JSON-object data: event of a chat/completions SSE stream."""
    for line in _SSE_NEWLINE_RE.split(sse_text):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):     # non-object event -> skip
            continue
        yield obj


def _iter_stream_tool_call_deltas(sse_text: str):
    """Yield every tool_call delta from a chat/completions SSE stream, in order."""
    for obj in _iter_sse_data_objects(sse_text):
        for ch in (obj.get("choices") or []):
            if not isinstance(ch, dict):
                continue
            ci = ch.get("index")                          # choice index (n>1 sampling)
            ci = ci if (isinstance(ci, int) and not isinstance(ci, bool)) else 0
            d = ch.get("delta")
            if not isinstance(d, dict):
                continue
            for tc in (d.get("tool_calls") or []):
                if isinstance(tc, dict):
                    yield (ci, tc)


def _capture_tool_sigs_stream(sse_text: str) -> None:
    for slot in _merge_tool_call_deltas(_iter_stream_tool_call_deltas(sse_text)):
        sid = slot.get("id")
        if isinstance(sid, str) and sid and slot.get("sig"):
            _THOUGHT_SIGS[sid] = slot["sig"]


def _reinject_tool_sigs(messages) -> None:
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            tcid = tc.get("id")
            # Non-string ids are unhashable and never have a stashed signature.
            if not isinstance(tcid, str):
                continue
            has = ((tc.get("extra_content") or {}).get("google") or {}).get("thought_signature")
            if not has and tcid in _THOUGHT_SIGS:
                tc.setdefault("extra_content", {}).setdefault("google", {})["thought_signature"] = _THOUGHT_SIGS[tcid]


# Gemini sentinel that skips signature validation; last resort for a turn's first call.
_SKIP_SIG = "skip_thought_signature_validator"


def _has_sig(tc) -> bool:
    return bool(((tc.get("extra_content") or {}).get("google") or {})
                .get("thought_signature"))


def _set_sig(tc, sig) -> None:
    tc.setdefault("extra_content", {}).setdefault("google", {})["thought_signature"] = sig


def _seen_sig_traffic(messages) -> bool:
    """True once thought_signature traffic has been seen (gates the skip-sentinel fallback)."""
    if _THOUGHT_SIGS:
        return True
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        for tc in (m.get("tool_calls") or []):
            if _has_sig(tc):
                return True
    return False


def _is_tool_call_only_assistant(m) -> bool:
    """Assistant message whose payload is purely tool_calls (no real text)."""
    if not isinstance(m, dict) or m.get("role") != "assistant":
        return False
    if not m.get("tool_calls"):
        return False
    c = m.get("content")
    return c is None or (isinstance(c, str) and not c.strip())


def _is_tool_call_assistant(m) -> bool:
    """Assistant message that carries tool_calls, with or without text."""
    return (isinstance(m, dict) and m.get("role") == "assistant"
            and bool(m.get("tool_calls")))


def _coalesce_assistant_tool_calls(messages):
    """Merge directly adjacent assistant messages that both carry tool_calls."""
    out = []
    for m in messages or []:
        if _is_tool_call_assistant(m) and out and _is_tool_call_assistant(out[-1]):
            prev = dict(out[-1])
            prev["tool_calls"] = list(prev.get("tool_calls") or []) + list(m.get("tool_calls") or [])
            # Keep the first message's content; append the second's text if it differs.
            c_prev, c_cur = prev.get("content"), m.get("content")
            if isinstance(c_cur, str) and c_cur.strip() and c_cur != c_prev:
                if isinstance(c_prev, str) and c_prev.strip():
                    prev["content"] = c_prev + "\n" + c_cur
                elif not (isinstance(c_prev, str) and c_prev.strip()):
                    prev["content"] = c_cur
            out[-1] = prev
        else:
            out.append(m)
    return out


def _normalize_sig_messages(messages):
    """Rewrite messages for Gemini thought signatures: coalesce, reinject, and backfill."""
    msgs = _coalesce_assistant_tool_calls(messages)
    _reinject_tool_sigs(msgs)
    if _seen_sig_traffic(msgs):
        for m in msgs:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            first = (m.get("tool_calls") or [None])[0]
            # Gemini requires only the first call of each model turn to be signed.
            if isinstance(first, dict) and not _has_sig(first):
                _set_sig(first, _SKIP_SIG)
    return msgs


# Params some strict chat/completions upstreams reject.
_BAD_PARAMS = {
    "reasoningSummary", "reasoning_summary",
    "reasoningEffort",  # strip harness defaults before validated AWM injection
    "reasoning",
}


def _sanitize_chat_body(body: dict) -> dict:
    """Strip rejected params and normalize the output-token parameter."""
    if not isinstance(body, dict):
        return {}
    out = dict(body)
    # Non-strict models: forward verbatim except finite-sanitize and reasoning injection.
    if not _is_strict_gpt5_family(out.get("model")):
        # A configured reasoning policy overrides conflicting client aliases.
        if REASONING_EFFORT:
            for k in _BAD_PARAMS:
                out.pop(k, None)
        for _k, _v in list(out.items()):
            if isinstance(_v, (dict, list)):
                out[_k] = _finite_sanitize(_v)
        # Some upstreams outside the strict GPT-5 family still require max_tokens.
        if _chat_prefers_max_tokens():
            if "max_completion_tokens" in out:
                out.setdefault("max_tokens", out["max_completion_tokens"])
                out.pop("max_completion_tokens", None)
        elif CHAT_TOKEN_LIMIT_PARAM == "max_completion_tokens" and "max_tokens" in out:
            out.setdefault("max_completion_tokens", out.pop("max_tokens"))
        _degrade_images_for_text_model(out.get("messages"), out.get("model"))
        _inject_reasoning_chat(out)
        return out
    for k in list(out.keys()):
        if k in _BAD_PARAMS:
            out.pop(k, None)
    # Map non-finite floats in structured fields to null; top-level scalars pass through.
    for _k, _v in list(out.items()):
        if isinstance(_v, (dict, list)):
            out[_k] = _finite_sanitize(_v)
    # GPT-5-style endpoints reject max_tokens while others need it; normalize by upstream family.
    if _chat_prefers_max_tokens():
        if "max_completion_tokens" in out:
            out.setdefault("max_tokens", out["max_completion_tokens"])
            out.pop("max_completion_tokens", None)
    elif "max_tokens" in out:
        out.setdefault("max_completion_tokens", out.pop("max_tokens"))
    # GPT-5 reasoning models only accept temperature=1.
    if "temperature" in out and out.get("temperature") not in (1, 1.0, None):
        out.pop("temperature", None)
    _degrade_images_for_text_model(out.get("messages"), out.get("model"))
    _inject_reasoning_chat(out)
    return out


def _flatten_content(content) -> str:
    return _content_text_safe(content)


def chat_to_responses_request(chat_req: dict) -> dict:
    """Translate an OpenAI /chat/completions request → /responses request."""
    instructions = []
    instruction_imgs = []   # renderable images on system messages
    input_items = []
    # Repair tool pairing first: reorder results after their calls, demote orphans to user.
    _msgs = _repair_tool_pairing(chat_req.get("messages") or [])
    # Null-id tool_calls get collision-proof placeholders, paired FIFO with null-id results.
    _existing_ids: set = set()
    _collect_tool_ids(_msgs, _existing_ids)
    _next_call_id = _make_unique_id_minter(_existing_ids)
    _null_id_q: list = []

    def _call_id_for_call(raw) -> str:
        s = _usable_id_str(raw)   # non-string id -> mint, never json.dumps
        if s:
            return s
        pid = _next_call_id()
        _null_id_q.append(pid)
        return pid

    def _call_id_for_output(raw) -> str:
        s = _usable_id_str(raw)   # non-string id -> FIFO-pair/mint
        if s:
            return s
        return _null_id_q.pop(0) if _null_id_q else _next_call_id()

    # Queue tool-result images; flush after the whole tool-output run to keep pairing valid.
    _deferred_tool_imgs: list = []

    def _flush_tool_imgs():
        if _deferred_tool_imgs:
            input_items.extend(_deferred_tool_imgs)
            _deferred_tool_imgs.clear()

    for m in _msgs:
        role = m.get("role", "user")
        if role != "tool":
            _flush_tool_imgs()
        if role == "system":
            instructions.append(_flatten_content(m.get("content")))
            # System-message images can't ride the text-only `instructions`; re-emit them below.
            c = m.get("content")
            if isinstance(c, list):
                instruction_imgs.extend(_iter_render_images(c, "input_image"))
        elif role == "tool":
            # Resolve call_id once; reuse it for the output and the tool-result image label.
            _oid = _call_id_for_output(m.get("tool_call_id"))
            input_items.append({
                "type": "function_call_output",
                "call_id": _oid,
                "output": _flatten_content(m.get("content")),
            })
            # Tool-result images can't ride function_call_output; defer them until after the batch.
            tool_imgs = list(_iter_render_images(m.get("content"), "input_image"))
            if tool_imgs:
                _deferred_tool_imgs.append({
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text",
                                 "text": "[image returned by tool call "
                                         f"{_oid}]"}] + tool_imgs,
                })
        elif role == "assistant":
            text = _flatten_content(m.get("content"))
            if text:
                input_items.append({
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                })
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {})
                # Arguments always a valid JSON string; call_id never null.
                input_items.append({
                    "type": "function_call",
                    "call_id": _call_id_for_call(tc.get("id")),
                    "name": _coerce_text(fn.get("name")) or "tool",
                    "arguments": _tool_args_json(fn.get("arguments")),
                })
        else:  # user and other roles -> input message
            content = m.get("content")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if not isinstance(p, dict):
                        # Sub-array/scalar element: salvage nested images, else coerced text.
                        deep = list(_iter_render_images(p, "input_image"))
                        if deep:
                            parts.extend(deep)
                        else:
                            parts.append({"type": "input_text",
                                          "text": _coerce_text(p)})
                    elif p.get("type") == "image_url":
                        iu = p.get("image_url")
                        url = iu.get("url") if isinstance(iu, dict) else iu
                        parts.append({"type": "input_image", "image_url": url})
                    elif p.get("type") == "input_text":
                        # The /responses schema requires `text` to be a string.
                        parts.append({"type": "input_text",
                                      "text": _coerce_text(p.get("text"))})
                    elif p.get("type") == "input_image":
                        parts.append(p)
                    elif p.get("type") == "image":
                        # Anthropic-style image block: re-emit as an image, not repr text.
                        parts.extend(_iter_render_images(p, "input_image"))
                    else:
                        parts.append({"type": "input_text",
                                      "text": _coerce_text(p.get("text"))})
                input_items.append({
                    "type": "message", "role": "user", "content": parts})
            else:
                input_items.append({
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text",
                                 "text": _flatten_content(content)}],
                })

    # Flush tool-result images whose batch ended at the last message.
    _flush_tool_imgs()

    # Attach system images to the first user input message, else prepend one.
    if instruction_imgs:
        target = None
        for it in input_items:
            if isinstance(it, dict) and it.get("type") == "message" \
                    and it.get("role") == "user":
                target = it
                break
        if target is not None and isinstance(target.get("content"), list):
            target["content"] = list(target["content"]) + list(instruction_imgs)
        else:
            input_items.insert(0, {"type": "message", "role": "user",
                                   "content": list(instruction_imgs)})

    # Backstop: re-emit any captured image missing from the built input items.
    _forward_responses_input_images(chat_req, input_items)

    rreq = {"model": chat_req.get("model") or OPENAI_MODEL, "input": input_items}
    joined = "\n\n".join(p for p in instructions if p)
    if joined:
        rreq["instructions"] = joined
    if chat_req.get("tools"):
        # strict must be explicitly false: unset may act strict; true 400s on MCP schemas.
        rreq["tools"] = [
            {
                "type": "function",
                # Normalize null/invalid definition fields; /responses tool defs are flat.
                **_tool_def_fields(
                    (t.get("function") or {}).get("name"),
                    (t.get("function") or {}).get("description"),
                    (t.get("function") or {}).get("parameters", {"type": "object"})),
                "strict": False,
            }
            for t in chat_req["tools"] if t.get("type") == "function"
        ]
    if isinstance(chat_req.get("reasoning"), dict):
        rreq["reasoning"] = _finite_sanitize(chat_req["reasoning"])
    elif chat_req.get("reasoning_effort"):
        rreq["reasoning"] = {"effort": str(chat_req["reasoning_effort"])}
    _inject_reasoning_responses(rreq)
    return rreq


def _sanitize_responses_body(rreq: dict) -> dict:
    out = dict(rreq)
    for k in ("max_tokens", "max_completion_tokens"):
        if k in out:
            out.setdefault("max_output_tokens", out.pop(k))
    # Reasoning models on /responses only accept temperature=1 (or unset).
    if out.get("temperature") not in (1, 1.0, None):
        out.pop("temperature", None)
    for k in list(out.keys()):
        if k in _BAD_PARAMS:
            out.pop(k, None)
    _inject_reasoning_responses(out)
    return out


def responses_to_chat_response(rj: dict) -> dict:
    """Translate an upstream /responses payload → /chat/completions response."""
    text_parts = []
    tool_calls = []
    # Mint unique 'call_N' ids for function_calls with neither call_id nor id.
    _rids: set = set()
    for _it in (rj.get("output") or []):
        if isinstance(_it, dict):
            for _kk in ("call_id", "id"):
                _vv = _it.get(_kk)
                if isinstance(_vv, str) and _vv:
                    _rids.add(_vv)
    _mint_rid = _make_unique_id_minter(_rids)
    for item in (rj.get("output") or []):
        if not isinstance(item, dict):
            continue
        it = item.get("type")
        if it == "message":
            # Null or scalar content must not crash the loop; coerce to [].
            _content = item.get("content")
            if not isinstance(_content, list):
                _content = []
            for block in _content:
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    # No 'None' for null text, no literal '[]'/'{}' for empty containers.
                    text_parts.append(_coerce_text_field(block.get("text")))
        elif it == "function_call":
            # Arguments -> valid JSON string; name -> non-null string.
            tool_calls.append({
                "id": _usable_id_str(item.get("call_id")) or _usable_id_str(item.get("id")) or _mint_rid(),
                "type": "function",
                "function": {
                    "name": _coerce_text(item.get("name")) or "tool",
                    "arguments": _tool_args_json(item.get("arguments")),
                },
            })
    if not text_parts and isinstance(rj.get("output_text"), str):
        text_parts.append(rj["output_text"])

    # Some /responses upstreams return a chat.completion body; fall back to choices[0].
    if not text_parts and not tool_calls:
        ch = (rj.get("choices") or [{}])
        cm = ch[0].get("message", {}) if isinstance(ch[0], dict) else {}
        if isinstance(cm, dict):
            ctext = cm.get("content")
            _ct = _content_text_safe(ctext) if ctext is not None else ""   # empty container -> ''
            if _ct:
                text_parts.append(_ct)
            for tc in (cm.get("tool_calls") or []):
                if _is_usable_tool_call(tc):
                    fn = tc.get("function", {}) or {}
                    tool_calls.append({
                        "id": _usable_id_str(tc.get("id")) or _usable_id_str(tc.get("call_id")) or _mint_rid(),
                        "type": "function",
                        "function": {
                            "name": _coerce_text(fn.get("name")) or "tool",
                            "arguments": _tool_args_json(fn.get("arguments")),
                        },
                    })

    message = {"role": "assistant",
               "content": "\n".join(t for t in text_parts if t) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    u = rj.get("usage") or {}
    return {
        "id": rj.get("id", "resp_proxy"),
        "object": "chat.completion",
        "model": rj.get("model", OPENAI_MODEL),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": {
            "prompt_tokens": u.get("input_tokens", 0),
            "completion_tokens": u.get("output_tokens", 0),
            "total_tokens": u.get("total_tokens", 0),
        },
    }


def _post_responses(chat_req: dict) -> dict:
    """POST a chat-shaped request to the upstream /responses API; return a chat-shaped response."""
    rreq = _sanitize_responses_body(chat_to_responses_request(chat_req))
    data = json.dumps(rreq).encode("utf-8")
    raw = _urlopen_retry(
        lambda: urllib.request.Request(
            _responses_url(), data=data, method="POST", headers=_auth_headers()),
        timeout=LLM_PROXY_UPSTREAM_TIMEOUT_S)
    rj = json.loads(raw)
    return _attach_request_params(responses_to_chat_response(rj), rreq, "responses")


def _aggregate_chat_stream(raw_text: str) -> dict:
    """Aggregate a /chat/completions SSE stream into one non-streaming chat.completion dict."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tc_deltas: list[dict] = []           # tool_call deltas, in arrival order
    finish_reason = None
    rid = model = None
    usage: dict = {}
    role = "assistant"
    # Splits on real newlines only and skips non-object data lines.
    for obj in _iter_sse_data_objects(raw_text):
        rid = rid or obj.get("id")
        model = model or obj.get("model")
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
        for ch in (obj.get("choices") or []):
            if not isinstance(ch, dict):
                continue
            _ci = ch.get("index")                         # choice index (n>1 sampling)
            _ci = _ci if (isinstance(_ci, int) and not isinstance(_ci, bool)) else 0
            d = ch.get("delta")
            if not isinstance(d, dict):
                d = {}
            # Content/role/finish_reason come from choice 0 only, so n>1 samples aren't concatenated.
            if _ci == 0:
                if d.get("role"):
                    role = d["role"]
                if d.get("content"):
                    # Keep only visible text; hidden reasoning blocks must not leak into content.
                    content_parts.append(_visible_text(d.get("content")))
                reasoning_delta = d.get("reasoning_content") or d.get("reasoning")
                if reasoning_delta:
                    reasoning_parts.append(_coerce_text(reasoning_delta))
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
            # Tool calls are kept per (choice, index) so n>1 samples stay distinct.
            for _tc in (d.get("tool_calls") or []):
                if isinstance(_tc, dict):
                    tc_deltas.append((_ci, _tc))
    message = {"role": role, "content": "".join(content_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    tool_calls = _merge_tool_call_deltas(tc_deltas)
    if tool_calls:
        message["tool_calls"] = [
            {"id": s["id"], "type": "function",
             "function": {"name": s["name"], "arguments": s["arguments"]}}
            for s in tool_calls]
    return {
        "id": rid or "chatcmpl-proxy",
        "object": "chat.completion",
        "model": model or OPENAI_MODEL,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
        }],
        "usage": usage,
    }


def _looks_like_sse(text: str) -> bool:
    """True if the body starts like an SSE stream (data:, event: or a ':' comment)."""
    head = (text or "").lstrip()[:64]
    return head.startswith("data:") or head.startswith("event:") or head.startswith(":")


def _post_openai_verbatim(req: dict) -> dict:
    """Verbatim passthrough for plain OpenAI-compatible servers: single attempt, no field drops."""
    body = dict(req)
    streaming = bool(body.get("stream"))
    data = _json_dumps_safe(body).encode("utf-8")
    request = urllib.request.Request(
        _chat_url(), data=data, method="POST", headers=_auth_headers())
    try:
        raw = _urlopen_retry(
            lambda: request,
            timeout=LLM_PROXY_UPSTREAM_TIMEOUT_S,
            slept_box=[0.0], drain=False)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", "ignore")
        rejected = _unknown_name_field(error_body)
        if e.code == 400 and rejected and _explicit_reasoning_field(rejected):
            raise _reasoning_strip_error(e, error_body, rejected)
        raise urllib.error.HTTPError(
            e.url, e.code, e.reason, e.headers,
            io.BytesIO(error_body.encode("utf-8")),
        )
    text = raw.decode("utf-8", "ignore")
    if streaming and _looks_like_sse(text):
        _capture_tool_sigs_stream(text)
        return _attach_request_params(_aggregate_chat_stream(text), body, "chat")
    resp_json = json.loads(text)
    _capture_tool_sigs(resp_json)
    return _attach_request_params(resp_json, body, "chat")


def _post_openai(req: dict) -> dict:
    """Central upstream call; returns a /chat/completions-shaped response."""
    _degrade_images_for_text_model(req.get("messages"), req.get("model"))
    if _USE_RESPONSES:
        return _post_responses(req)
    # Other models: forward verbatim; the rewrites below can corrupt large bodies.
    if not _is_strict_gpt5_family(req.get("model")):
        return _post_openai_verbatim(req)
    if req.get("messages") is not None:
        req["messages"] = _normalize_sig_messages(req["messages"])
    streamed = dict(req)
    streamed["stream"] = True
    streamed.setdefault("stream_options", {"include_usage": True})

    # One retry-sleep budget shared across field-drop retries; drain once a field is dropped.
    _slept_box = [0.0]
    _dropped = [False]

    def _send(body: dict) -> bytes:
        data = json.dumps(body).encode("utf-8")
        return _urlopen_retry(
            lambda: urllib.request.Request(
                _chat_url(), data=data, method="POST", headers=_auth_headers()),
            timeout=LLM_PROXY_UPSTREAM_TIMEOUT_S,
            slept_box=_slept_box, drain=_dropped[0])

    # Upstreams may 400 on fields they don't support; drop the offending field and retry.
    raw = None
    for _ in range(6):
        try:
            raw = _send(streamed)
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            if _looks_responses_only(e.code, body):
                sys.stderr.write("[proxy] upstream is /responses-only; switching API\n")
                _enable_responses_mode()
                return _post_responses(req)
            if e.code == 400 and "stream_options" in body and "stream_options" in streamed:
                streamed.pop("stream_options", None)
                _dropped[0] = True
                continue
            if e.code == 400:
                dropped = _unknown_name_field(body)
                if dropped and dropped in streamed:
                    if _explicit_reasoning_field(dropped):
                        raise _reasoning_strip_error(e, body, dropped)
                    sys.stderr.write(f"[proxy] upstream rejected field {dropped!r}; "
                                     f"retrying without it\n")
                    streamed.pop(dropped, None)
                    _dropped[0] = True
                    continue
            raise urllib.error.HTTPError(
                e.url, e.code, e.reason, e.headers, io.BytesIO(body.encode("utf-8")))
    if raw is None:
        raise RuntimeError("upstream call exhausted field-drop retries")

    text = raw.decode("utf-8", "ignore")
    # Detect SSE by its head; a JSON body despite stream=true falls through to json.loads.
    if _looks_like_sse(text):
        _capture_tool_sigs_stream(text)
        return _attach_request_params(_aggregate_chat_stream(text), streamed, "chat")
    # Upstream ignored stream and returned a plain JSON body — use it as-is.
    resp_json = json.loads(text)
    _capture_tool_sigs(resp_json)
    return _attach_request_params(resp_json, streamed, "chat")


def _retryable_empty_assistant_response(response: dict) -> bool:
    try:
        choice = (response.get("choices") or [{}])[0]
        if not isinstance(choice, dict):
            return False
        if choice.get("finish_reason") not in (None, "stop"):
            return False
        output = _extract_output(response)
        return not str(output.get("content") or "").strip() \
            and not (output.get("tool_calls") or [])
    except Exception:
        return False


def _post_openai_with_empty_retries(req: dict) -> dict:
    response = _post_openai(req)
    try:
        retry_limit = max(
            0,
            min(10, int(os.environ.get("PROXY_EMPTY_RESPONSE_RETRIES", "4"))),
        )
    except (TypeError, ValueError):
        retry_limit = 4
    for retry_index in range(1, retry_limit + 1):
        if not _retryable_empty_assistant_response(response):
            break
        sys.stderr.write(
            "[proxy] empty assistant response "
            f"({_chat_response_summary(response)}); retry "
            f"{retry_index}/{retry_limit}\n"
        )
        retry_req = dict(req)
        retry_req["messages"] = list(req.get("messages") or []) + [{
            "role": "user",
            "content": (
                f"Compatibility retry {retry_index}: your previous generation "
                "contained only private reasoning and no visible assistant text "
                "or tool call. Continue the latest task now. If work remains, "
                "emit a valid tool call in this response; if all work is complete, "
                "emit a concise visible final answer. Do not mention this retry."
            ),
        }]
        response = _post_openai(retry_req)
    return response


def responses_to_chat(req: dict) -> dict:
    """Translate a /responses request to /chat/completions with one leading system message."""
    input_items = req.get("input", [])
    # A string or dict `input` is a single message; don't iterate it.
    if isinstance(input_items, str):
        input_items = [input_items]
    elif isinstance(input_items, dict):
        input_items = [input_items]
    elif not isinstance(input_items, list):
        input_items = [] if input_items is None else [input_items]
    system_parts = []
    if req.get("instructions"):
        # Non-string instructions must not crash the join below.
        ins = req["instructions"]
        if isinstance(ins, list):
            system_parts.extend(_coerce_text(p) for p in ins)
        else:
            system_parts.append(_coerce_text(ins))
    # Mint unique placeholder ids for id-less call/output pairs; outputs pair FIFO.
    _existing_ids: set = set()
    _collect_tool_ids(input_items, _existing_ids)
    _next_call_id = _make_unique_id_minter(_existing_ids)
    _null_id_q: list = []
    body = []  # non-system turns, in order
    for item in input_items:
        if isinstance(item, dict):
            t = item.get("type")
            # `type` is optional on input messages; a typeless item is a message.
            if t in (None, "message"):
                content = item.get("content", "")
                role = item.get("role", "user")
                if _is_content_block_dict(content):
                    # Wrap a bare content-block dict in a list so it flattens like one.
                    content = [content]
                # Find images at any depth.
                deep_imgs = (list(_iter_render_images(content, "image_url"))
                             if isinstance(content, list) else [])
                if deep_imgs and role not in ("developer", "system"):
                    # Keep input_image as image_url parts: text first, then every image.
                    text = _flatten_block_text(content)
                    parts = ([{"type": "text", "text": text}] if text else []) \
                        + deep_imgs
                    body.append({"role": role, "content": parts})
                    continue
                if isinstance(content, list):
                    content = _flatten_block_text(content)
                else:
                    # Dict content keeps its JSON value with image bytes stripped.
                    content = _content_text_safe(content)
                if role in ("developer", "system"):
                    system_parts.append(content)
                else:
                    body.append({"role": role, "content": content})
            elif t == "function_call":
                # Pair via call_id, falling back to id; if both are absent mint and enqueue a placeholder.
                _cid = item.get("call_id") or item.get("id")
                if not _cid:
                    _cid = _next_call_id()
                    _null_id_q.append(_cid)
                tc = {
                    "id": _cid,
                    "type": "function",
                    "function": {
                        "name": _coerce_text(item.get("name")) or "tool",
                        "arguments": _tool_args_json(item.get("arguments")),
                    },
                }
                # Consecutive function_calls are one batch; splitting them breaks Gemini thought signatures.
                prev = body[-1] if body else None
                if prev is not None and prev.get("role") == "assistant" \
                        and prev.get("tool_calls"):
                    prev["tool_calls"].append(tc)
                else:
                    body.append({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [tc],
                    })
            elif t == "function_call_output":
                # Output coerced to text; tool_call_id falls back from call_id to id.
                _oid = item.get("call_id") or item.get("id")
                if not _oid:
                    # FIFO-pair with the placeholder minted for the matching function_call.
                    _oid = _null_id_q.pop(0) if _null_id_q else _next_call_id()
                body.append({
                    "role": "tool",
                    "tool_call_id": _oid,
                    # Flatten list/dict output to text without leaking base64.
                    "content": _content_text_safe(item.get("output")),
                })
            elif t in ("reasoning", "item_reference"):
                # Reasoning and item_reference items are internal; never forward them.
                continue
            elif "role" in item or "content" in item:
                # Unknown item types with role/content are kept as messages, not dropped.
                role = item.get("role", "user")
                content = item.get("content", "")
                if isinstance(content, list):
                    content = _flatten_block_text(content)
                else:
                    content = _coerce_text(content)
                if role in ("developer", "system"):
                    system_parts.append(content)
                else:
                    body.append({"role": role, "content": content})
        elif isinstance(item, str):
            body.append({"role": "user", "content": item})

    messages = []
    # Skip the system message when every part is empty.
    if any(p for p in system_parts):
        messages.append({"role": "system",
                         "content": "\n\n".join(p for p in system_parts if p)})
    messages.extend(body)
    # Repair tool-call/tool-result adjacency before sending upstream.
    messages = _repair_tool_pairing(messages)

    chat_req = {"model": _pick_model(req), "messages": messages}
    # Omit `tools` when empty or when no function tools remain.
    if req.get("tools"):
        _tools = []
        for t in req["tools"]:
            if isinstance(t, dict) and t.get("type") == "function":
                _tools.append({
                    "type": "function",
                    # Normalize null/invalid tool definition fields.
                    "function": _tool_def_fields(
                        t.get("name"), t.get("description"),
                        t.get("parameters", {"type": "object"})),
                })
        if _tools:
            chat_req["tools"] = _tools
    if isinstance(req.get("reasoning"), dict):
        chat_req["reasoning"] = _finite_sanitize(req["reasoning"])
    elif isinstance(req.get("reasoning"), str):
        chat_req["reasoning_effort"] = req["reasoning"]
    _inject_reasoning_chat(chat_req)
    return chat_req


def chat_to_responses(resp: dict, model: str) -> dict:
    """Translate /chat/completions response → /responses format."""
    # Degenerate choices/messages/tool_calls degrade to an empty turn instead of crashing.
    choice = (resp.get("choices") or [{}])[0]
    if not isinstance(choice, dict):
        choice = {}
    msg = choice.get("message")
    if not isinstance(msg, dict):
        msg = {}
    # Content may be a block list; output_text needs a string.
    _content = msg.get("content")
    # Keep visible text only; hidden reasoning blocks must not leak.
    text = _visible_text(_content)
    _tcs = msg.get("tool_calls")
    tool_calls = _tcs if isinstance(_tcs, list) else []

    output = []
    if text:
        output.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
            "status": "completed",
        })
    for tc in tool_calls:
        if not _is_usable_tool_call(tc):
            continue   # skip null, non-dict or empty tool_calls
        fn = tc.get("function", {})
        # String args pass through unchanged; only non-string args are coerced to JSON.
        _args = fn.get("arguments")
        output.append({
            "type": "function_call",
            "id": tc.get("id"),
            "call_id": tc.get("id"),
            "name": _coerce_text(fn.get("name")) or "tool",
            "arguments": _args if isinstance(_args, str) else _tool_args_json(_args),
            "status": "completed",
        })

    usage = resp.get("usage")          # null or non-dict -> {}
    usage = usage if isinstance(usage, dict) else {}
    return {
        "id": resp.get("id", "resp_proxy"),
        "object": "response",
        "model": model,
        "output": output,
        "status": "completed",
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[proxy] {fmt % args}\n")

    def handle_one_request(self):
        # The client may hang up mid-response; ignore the broken pipe.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_json(self, code, obj):
        # Never emit bare NaN/Infinity; strict clients reject the body.
        body = _json_dumps_safe(obj).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Client already gone; give up.
            self.close_connection = True

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        if path in ("/", "/health"):
            self._send_json(200, {
                "status": "ok",
                "service": "anthropic-to-openai-proxy",
                "model": OPENAI_MODEL,
                "model_ref": PROXY_MODEL_REF,
                "base_url": OPENAI_BASE_URL,
                "pin_model": PIN_MODEL,
                "reasoning_effort": REASONING_EFFORT or None,
                "reasoning_chat_param": REASONING_CHAT_PARAM,
                "reasoning_responses_param": REASONING_RESPONSES_PARAM,
            })
        elif path == "/stats":
            # Call/image/step counts; ?reset=1 clears them, ?rawdbg=1|0 toggles raw body capture.
            global _DEBUG_RAW
            do_reset = "reset=1" in query
            if "rawdbg=1" in query:
                _DEBUG_RAW = True
            elif "rawdbg=0" in query:
                _DEBUG_RAW = False
            # Read counters before resetting; the reset itself is atomic in _reset_trajectory().
            with _CALL_LOCK:
                n, imgs = _CALL_COUNT, _IMG_N
            if do_reset:
                _reset_trajectory()
            with _TRAJ_LOCK:
                n_sessions = len(_TRAJ_STEPS)
                max_step = _TRAJ_MAX_STEP
            self._send_json(200, {"llm_calls": n, "images": imgs,
                                  "image_dir": IMAGE_DIR,
                                  "steps": 0 if do_reset else max_step,
                                  "sessions": 0 if do_reset else n_sessions,
                                  "traj_dir": TRAJ_DIR, "rawdbg": _DEBUG_RAW})
        elif path == "/trajectory":
            # Per-step trajectory; ?session=<fp> returns one session, else all grouped by fingerprint.
            qs = urllib.parse.parse_qs(query)
            sess = (qs.get("session") or [None])[0]
            with _TRAJ_LOCK:
                if sess is not None:
                    steps = [_public_step(r) for r in _TRAJ_STEPS.get(sess, [])]
                    self._send_json(200, {"session": sess, "steps": steps})
                else:
                    out = {fp: [_public_step(r) for r in recs]
                           for fp, recs in _TRAJ_STEPS.items()}
                    self._send_json(200, {"sessions": out})
        else:
            self._send_json(404, {"error": "not found"})

    def _send_sse_event(self, event_type, data):
        """Send a single SSE event chunk."""
        # Never emit bare NaN/Infinity in streamed events.
        line = f"event: {event_type}\ndata: {_json_dumps_safe(data)}\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    def _send_anthropic_message_sse(self, message):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for event_type, data in _anthropic_message_sse_events(message):
            self._send_sse_event(event_type, data)
        self.close_connection = True

    def _handle_responses(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            _debug_raw_dump("/responses", raw, self.headers)  # opt-in
            req = json.loads(raw) if raw else {}
            # Null input/tools -> []; a non-dict body is left to fail as a 5xx.
            if isinstance(req, dict):
                if not isinstance(req.get("input"), (list, str, dict)):
                    req["input"] = []
                if not isinstance(req.get("tools"), list):
                    req["tools"] = []
            imgs = _scan_images(req)
            stream = req.get("stream", False)
            sys.stderr.write(f"[proxy] /responses: "
                             f"{len(req.get('input') or [])} inputs, "
                             f"tools={len(req.get('tools') or [])}, stream={stream}\n")
            chat_req = responses_to_chat(req)
            # Backstop: forward every captured image missing from the upstream body.
            _ensure_images_forwarded(req, chat_req.get("messages"))
            chat_resp = _post_openai(chat_req)
            # Convert before logging so an unconvertible body logs no step.
            resp = chat_to_responses(chat_resp, req.get("model", OPENAI_MODEL))
            # Log in canonical chat form so all harnesses share one schema.
            _log_step(chat_req.get("messages"), chat_resp, imgs)

            if stream:
                # SSE streaming response — Codex expects this.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                # Event sequence per Responses API streaming protocol
                self._send_sse_event("response.created", {
                    "type": "response.created",
                    "sequence_number": 0,
                    "response": {**resp, "output": [], "status": "in_progress"},
                })

                seq = 1
                for idx, item in enumerate(resp.get("output", [])):
                    self._send_sse_event("response.output_item.added", {
                        "type": "response.output_item.added",
                        "sequence_number": seq,
                        "output_index": idx,
                        "item": item,
                    })
                    seq += 1
                    # For text items, emit a delta + done
                    if item.get("type") == "message":
                        for cidx, content_block in enumerate(item.get("content", [])):
                            if content_block.get("type") == "output_text":
                                text = content_block.get("text", "")
                                self._send_sse_event("response.output_text.delta", {
                                    "type": "response.output_text.delta",
                                    "sequence_number": seq,
                                    "item_id": item.get("id", f"item_{idx}"),
                                    "output_index": idx,
                                    "content_index": cidx,
                                    "delta": text,
                                })
                                seq += 1
                                self._send_sse_event("response.output_text.done", {
                                    "type": "response.output_text.done",
                                    "sequence_number": seq,
                                    "item_id": item.get("id", f"item_{idx}"),
                                    "output_index": idx,
                                    "content_index": cidx,
                                    "text": text,
                                })
                                seq += 1
                    self._send_sse_event("response.output_item.done", {
                        "type": "response.output_item.done",
                        "sequence_number": seq,
                        "output_index": idx,
                        "item": item,
                    })
                    seq += 1

                self._send_sse_event("response.completed", {
                    "type": "response.completed",
                    "sequence_number": seq,
                    "response": resp,
                })
            else:
                self._send_json(200, resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            sys.stderr.write(f"[proxy] /responses HTTP {e.code}: {body[:500]}\n")
            self._send_json(e.code, {"error": {"type": "api_error", "message": body[:1000]}})
        except Exception as e:
            sys.stderr.write(f"[proxy] /responses error: {e}\n{traceback.format_exc()}\n")
            self._send_json(500, {"error": {"type": "internal", "message": str(e)}})

    def _send_chat_chunk(self, rid, model, delta, finish_reason=None):
        chunk = {
            "id": rid, "object": "chat.completion.chunk", "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        # Never emit bare NaN/Infinity in the synthesized chunk.
        self.wfile.write(f"data: {_json_dumps_safe(chunk)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _serve_chat_via_responses(self, body, images=None, canon_messages=None):
        """Serve a /chat/completions request via the upstream /responses API (JSON or SSE)."""
        try:
            chat_resp = _post_responses(body)
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", "ignore")
            sys.stderr.write(f"[proxy] /responses HTTP {e.code}: {err[:400]}\n")
            self._send_json(e.code, {"error": {"type": "api_error", "message": err[:1000]}})
            return
        # Log against the pre-field-drop messages snapshot when given.
        log_messages = canon_messages if canon_messages is not None else body.get("messages")
        _log_step(log_messages, chat_resp, images)
        if not body.get("stream"):
            self._send_json(200, chat_resp)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        choice = (chat_resp.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        rid = chat_resp.get("id", "chatcmpl-proxy")
        model = chat_resp.get("model", OPENAI_MODEL)
        self._send_chat_chunk(rid, model, {"role": "assistant"})
        if msg.get("content"):
            self._send_chat_chunk(rid, model, {"content": msg["content"]})
        for i, tc in enumerate(msg.get("tool_calls") or []):
            self._send_chat_chunk(rid, model, {"tool_calls": [{
                "index": i, "id": tc.get("id"), "type": "function",
                "function": tc.get("function", {}),
            }]})
        self._send_chat_chunk(rid, model, {}, finish_reason=choice.get("finish_reason", "stop"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _handle_chat_completions(self):
        """Passthrough for OpenAI chat/completions with param sanitizing and 400 auto-heal."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            _debug_raw_dump("/chat/completions", raw, self.headers)  # opt-in
            body = json.loads(raw) if raw else {}
            # Reject non-object bodies; sanitizing would fabricate a request from them.
            if not isinstance(body, dict):
                self._send_json(400, {"error": {"type": "invalid_request_error",
                    "message": "request body must be a JSON object"}})
                return
            imgs = _scan_images(body)
            body = _sanitize_chat_body(body)
            # Ensure `messages` is a list so every call logs a step.
            if not isinstance(body.get("messages"), list):
                body["messages"] = []
            if body.get("messages"):  # Gemini thought_signatures (skip empty)
                body["messages"] = _normalize_sig_messages(body["messages"])
            # Snapshot messages: the field-drop loop below pops fields from body in place.
            canon_messages = body.get("messages")
            streaming = bool(body.get("stream"))
            sys.stderr.write(f"[proxy] /chat/completions model={body.get('model')} "
                             f"stream={streaming} msgs={len(body.get('messages', []))}\n")

            if _USE_RESPONSES:
                self._serve_chat_via_responses(body, imgs, canon_messages)
                return

            # One retry-sleep budget for the whole field-drop loop; drain after the first drop.
            _slept_box = [0.0]
            _chat_dropped = [False]
            # Try, auto-healing on 400 unsupported-param up to 5 times.
            for _attempt in range(5):
                data = json.dumps(body).encode("utf-8")
                try:
                    # Retry 429/5xx before any bytes reach the client; read=False keeps streaming intact.
                    resp = _urlopen_retry(
                        lambda d=data: urllib.request.Request(
                            _chat_url(), data=d, method="POST",
                            headers=_auth_headers()),
                        timeout=LLM_PROXY_UPSTREAM_TIMEOUT_S, read=False,
                        slept_box=_slept_box, drain=_chat_dropped[0])
                except urllib.error.HTTPError as e:
                    errbody = e.read().decode("utf-8", "ignore")
                    if e.code == 400:
                        m = re.search(r"[Uu]nsupported (?:parameter|value): '([^']+)'", errbody) \
                            or re.search(r"[Uu]nknown parameter: '([^']+)'", errbody)
                        if m:
                            bad = m.group(1).split(".")[0]
                            if bad in body:
                                if _explicit_reasoning_field(bad):
                                    raise _reasoning_strip_error(e, errbody, bad)
                                body.pop(bad, None)
                                _chat_dropped[0] = True
                                sys.stderr.write(f"[proxy] auto-heal: stripped '{bad}', retrying\n")
                                continue
                        # Gemini 'Unknown name "<field>"': drop the named field and retry.
                        bad = _unknown_name_field(errbody)
                        if bad and bad in body:
                            if _explicit_reasoning_field(bad):
                                raise _reasoning_strip_error(e, errbody, bad)
                            body.pop(bad, None)
                            _chat_dropped[0] = True
                            sys.stderr.write(f"[proxy] upstream rejected field {bad!r}; "
                                             f"retrying without it\n")
                            continue
                        if _looks_responses_only(e.code, errbody):
                            sys.stderr.write("[proxy] chat/completions is /responses-only; switching\n")
                            _enable_responses_mode()
                            # Log against the pre-drop messages snapshot.
                            self._serve_chat_via_responses(body, imgs, canon_messages)
                            return
                    sys.stderr.write(f"[proxy] chat 400+: {errbody[:300]}\n")
                    self._send_json(e.code, json.loads(errbody) if errbody.strip().startswith("{") else {"error": {"message": errbody[:500]}})
                    return

                # Validate the upstream body before replying or logging a step.
                if streaming:
                    # Decide SSE vs JSON from the body head, not the advertised Content-Type.
                    first = b""
                    while True:
                        c = resp.read(8192)
                        if c is None:
                            break
                        first += c
                        if first.strip() or len(first) >= 8192:
                            break
                        if not c:
                            break
                    head = first.lstrip()[:64].decode("utf-8", "ignore").lower()
                    # SSE may start with data:, event: or a ':' keep-alive comment.
                    is_sse = (head.startswith("data:") or head.startswith("event:")
                              or head.startswith(":"))
                    if not is_sse:
                        # NOT a `data:`/`event:` head. Read the WHOLE body.
                        rest = b""
                        try:
                            while True:
                                c = resp.read(8192)
                                if not c:
                                    break
                                rest += c
                        except Exception:
                            pass
                        full = first + rest
                        if not full.strip():
                            # Empty body on a streaming 200: treat as an empty completion, not a 502.
                            sse_text = full.decode("utf-8", "ignore")
                            try:
                                _agg = _aggregate_chat_stream(sse_text)
                            except Exception:
                                _agg = {}
                            _log_step(canon_messages, _agg, imgs,
                                      request_body=body, wire_api="chat")
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Cache-Control", "no-cache")
                            self.end_headers()
                            try:
                                self.wfile.write(b"data: [DONE]\n\n")
                                self.wfile.flush()
                            except Exception:
                                pass
                            return
                        # Non-SSE body: try plain chat.completion JSON; 502 only if it is neither.
                        try:
                            resp_json = json.loads(full)
                        except Exception:
                            resp_json = None
                        if not isinstance(resp_json, dict):
                            # Unparseable or non-object body: 5xx before committing the 200.
                            bad = (full or b"")[:500].decode("utf-8", "ignore")
                            sys.stderr.write(f"[proxy] chat stream upstream neither SSE "
                                             f"nor a JSON object: {bad[:200]}\n")
                            self._send_json(502, {"error": {"type": "upstream_invalid",
                                "message": "upstream returned a body that is neither SSE "
                                           "nor a parseable JSON object on a streaming "
                                           "request: " + bad[:300]}})
                            return
                        # JSON body on a streaming request: synthesize an SSE stream and log the step.
                        try:
                            _capture_tool_sigs(resp_json)
                        except Exception:
                            pass
                        _log_step(canon_messages, resp_json, imgs,
                                  request_body=body, wire_api="chat")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        choice = (resp_json.get("choices") or [{}])[0] or {}
                        msg = choice.get("message", {}) or {}
                        rid = resp_json.get("id", "chatcmpl-proxy")
                        model = resp_json.get("model", OPENAI_MODEL)
                        self._send_chat_chunk(rid, model, {"role": "assistant"})
                        if msg.get("content"):
                            self._send_chat_chunk(rid, model, {"content": msg["content"]})
                        for i, tc in enumerate(msg.get("tool_calls") or []):
                            self._send_chat_chunk(rid, model, {"tool_calls": [{
                                "index": i, "id": tc.get("id"), "type": "function",
                                "function": tc.get("function", {}),
                            }]})
                        self._send_chat_chunk(rid, model, {},
                                              finish_reason=choice.get("finish_reason", "stop"))
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        return
                    # Buffer and log the whole stream before sending, so a kill can't lose a delivered step.
                    sse_buf = [first]
                    _read_ok = True
                    try:
                        while True:
                            chunk = resp.read(8192)
                            if not chunk:
                                break
                            sse_buf.append(chunk)
                    except Exception as _re:        # ConnectionReset / IncompleteRead mid-stream
                        _read_ok = False
                        sys.stderr.write(f"[proxy] chat stream upstream read error mid-stream: {_re}\n")
                    sse_text = b"".join(sse_buf).decode("utf-8", "ignore")
                    # A body shorter than its Content-Length is truncated even if read() didn't raise.
                    _truncated = False
                    try:
                        _cl = resp.headers.get("Content-Length")
                        if _cl is not None and sum(len(c) for c in sse_buf) < int(_cl):
                            _truncated = True
                    except Exception:
                        _truncated = False
                    if not _read_ok or _truncated:
                        # A mid-stream reset or short body is a 502 with no step; a torn tail is recovered later.
                        sys.stderr.write("[proxy] chat stream upstream truncated/reset mid-stream -> 502\n")
                        self._send_json(502, {"error": {"type": "upstream_invalid",
                            "message": "upstream SSE stream connection reset / incomplete read"}})
                        return
                    try:
                        _capture_tool_sigs_stream(sse_text)
                    except Exception:
                        pass
                    # Reassemble the stream and commit the step before any client byte.
                    try:
                        _agg = _aggregate_chat_stream(sse_text)
                    except Exception:
                        _agg = {}
                    _log_step(canon_messages, _agg, imgs,
                              request_body=body, wire_api="chat")
                    # Now (and only now) emit the buffered stream to the client.
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    for chunk in sse_buf:
                        self.wfile.write(chunk)
                    self.wfile.flush()
                else:
                    out = resp.read()
                    try:
                        resp_json = json.loads(out)
                    except Exception:
                        resp_json = None
                    if not isinstance(resp_json, dict):
                        # Not a JSON object: 502 and no step.
                        bad = (out or b"")[:500].decode("utf-8", "ignore")
                        sys.stderr.write(f"[proxy] chat upstream not a JSON object: {bad[:200]}\n")
                        self._send_json(502, {"error": {"type": "upstream_invalid",
                            "message": "upstream returned a non-object body: "
                                       + bad[:300]}})
                        return
                    try:
                        _capture_tool_sigs(resp_json)
                    except Exception:
                        pass
                    _log_step(canon_messages, resp_json, imgs,
                              request_body=body, wire_api="chat")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                return
            self._send_json(500, {"error": {"message": "auto-heal exhausted"}})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            sys.stderr.write(f"[proxy] /chat/completions HTTP {e.code}: {body[:500]}\n")
            self._send_json(e.code, {"error": {"type": "api_error", "message": body[:1000]}})
        except Exception as e:
            sys.stderr.write(f"[proxy] /chat/completions error: {e}\n{traceback.format_exc()}\n")
            try:
                self._send_json(500, {"error": {"type": "internal", "message": str(e)}})
            except Exception:
                pass

    def do_POST(self):
        # Strip query string for path matching (?beta=true etc.)
        path = self.path.split("?", 1)[0]
        # opencode (and generic OpenAI clients) → /chat/completions
        if path.endswith("/chat/completions"):
            _bump_calls()                       # one logical LLM turn
            self._handle_chat_completions()
            return
        # Codex sends to /v1/responses (Responses API)
        if path.endswith("/v1/responses") or path == "/responses":
            _bump_calls()                       # one logical LLM turn
            self._handle_responses()
            return
        if not (path.endswith("/v1/messages") or path.endswith("/v1/messages/count_tokens")):
            self._send_json(404, {"error": {"type": "not_found", "message": self.path}})
            return
        # /count_tokens is a local estimate, not an LLM call; don't count it.
        if not path.endswith("/count_tokens"):
            _bump_calls()
        if path.endswith("/count_tokens"):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                req = json.loads(raw) if raw else {}
                # Rough estimate: 1 token per 4 chars
                text = json.dumps(req)
                self._send_json(200, {"input_tokens": max(1, len(text) // 4)})
            except Exception as e:
                self._send_json(500, {"error": {"message": str(e)}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            _debug_raw_dump(path, raw, self.headers)   # opt-in
            req = json.loads(raw) if raw else {}
            # Null messages/tools -> []; a non-dict body is left to fail as a 5xx.
            if isinstance(req, dict):
                if req.get("messages") is None:
                    req["messages"] = []
                if req.get("tools") is None:
                    req["tools"] = []
            imgs = _scan_images(req)
            sys.stderr.write(f"[proxy] /v1/messages: "
                             f"{len(req.get('messages') or [])} msgs, "
                             f"tools={len(req.get('tools') or [])}\n")
            openai_req = anthropic_to_openai(req)
            # Backstop: forward every captured image missing from the upstream body.
            _ensure_images_forwarded(req, openai_req.get("messages"))
            t_up = time.time()
            openai_resp = _post_openai_with_empty_retries(openai_req)
            sys.stderr.write(
                f"[proxy] /v1/messages upstream "
                f"{time.time() - t_up:.1f}s {_chat_response_summary(openai_resp)}\n")
            # Convert (and validate) before logging so a call that will 5xx logs no step.
            anthropic_resp = openai_to_anthropic(openai_resp, req.get("model", OPENAI_MODEL))
            # Log the canonical request actually sent upstream plus the response.
            _log_step(openai_req.get("messages"), openai_resp, imgs)
            if req.get("stream"):
                self._send_anthropic_message_sse(anthropic_resp)
            else:
                self._send_json(200, anthropic_resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            sys.stderr.write(f"[proxy] HTTP {e.code}: {body[:500]}\n")
            self._send_json(e.code, {"error": {"type": "api_error", "message": body[:1000]}})
        except Exception as e:
            sys.stderr.write(f"[proxy] error: {e}\n")
            self._send_json(500, {"error": {"type": "internal", "message": str(e)}})


def main():
    if not OPENAI_BASE_URL or not OPENAI_API_KEY:
        sys.stderr.write("[proxy] AWM_OPENAI_BASE_URL and AWM_OPENAI_API_KEY must be set\n")
        sys.exit(1)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write(f"[proxy] listening on :{PORT} → {OPENAI_BASE_URL} (model={OPENAI_MODEL})\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
