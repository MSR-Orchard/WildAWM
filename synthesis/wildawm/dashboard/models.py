"""Provider-grouped model registry (models.json); models are referenced as `provider/model`."""

from pathlib import Path

from wildawm.const_config import MODELS_PATH
from wildawm.const_config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
from wildawm.json_utils import load_jsonc

VALID_PROVIDER_TYPES = {"azure_openai", "gemini", "openai_generic"}


class ModelError(ValueError):
    """Unknown provider/model, malformed registry, or missing credentials."""


def _registry_path() -> Path | None:
    return MODELS_PATH if MODELS_PATH.exists() else None


def _infer_provider_type(base_url: str) -> str:
    if "generativelanguage.googleapis.com" in (base_url or ""):
        return "gemini"
    return "azure_openai"


def _normalise_models(
    provider: str,
    raw_models,
    model_quirks: dict | None = None,
    model_info: dict | None = None,
) -> dict:
    """Normalise models to ``{name: {model_id, quirks, info}}`` (info holds harness capabilities)."""
    model_quirks = model_quirks or {}
    model_info = model_info or {}
    if not isinstance(model_quirks, dict):
        raise ModelError(f"provider {provider!r}: model_quirks must be an object")
    if not isinstance(model_info, dict):
        raise ModelError(f"provider {provider!r}: model_info must be an object")
    default_info = model_info.get("*", {}) or {}
    if not isinstance(default_info, dict):
        raise ModelError(
            f"provider {provider!r}: model_info['*'] must be an object")
    out: dict = {}
    if isinstance(raw_models, list):
        for name in raw_models:
            info = model_info.get(str(name), {}) or {}
            if not isinstance(info, dict):
                raise ModelError(
                    f"provider {provider!r}: model_info[{str(name)!r}] must be an object")
            out[str(name)] = {
                "model_id": None,
                "quirks": model_quirks.get(str(name), {}) or {},
                "info": {**default_info, **info},
            }
    elif isinstance(raw_models, dict):
        for name, m in raw_models.items():
            m = m or {}
            sparse = model_quirks.get(str(name), {}) or {}
            inline = m.get("quirks") or {}
            sparse_info = model_info.get(str(name), {}) or {}
            inline_info = m.get("info") or {}
            if not isinstance(sparse_info, dict) or not isinstance(inline_info, dict):
                raise ModelError(
                    f"provider {provider!r}: model info for {str(name)!r} must be an object")
            out[str(name)] = {
                "model_id": m.get("model_id"),
                "quirks": {**sparse, **inline},
                "info": {**default_info, **sparse_info, **inline_info},
            }
    else:
        raise ModelError(f"provider {provider!r}: models must be a list or dict")
    if not out:
        raise ModelError(f"provider {provider!r}: no models")
    return out


def _normalise_provider(name: str, raw: dict) -> dict:
    base_url = (raw.get("base_url") or "").strip()
    api_key = (raw.get("api_key") or "").strip()
    provider_type = (raw.get("provider_type") or _infer_provider_type(base_url)).strip()
    if provider_type not in VALID_PROVIDER_TYPES:
        raise ModelError(
            f"provider {name!r}: provider_type {provider_type!r} not in {sorted(VALID_PROVIDER_TYPES)}")
    if not base_url or not api_key:
        raise ModelError(f"provider {name!r}: base_url and api_key are required")
    return {
        "provider_type": provider_type,
        "base_url": base_url,
        "api_key": api_key,
        "models": _normalise_models(
            name,
            raw.get("models"),
            raw.get("model_quirks"),
            raw.get("model_info"),
        ),
    }


def load_registry() -> dict:
    """Return {"default": <ref|None>, "providers": {name: {...}}, "source": str}."""
    path = _registry_path()
    if path is not None:
        try:
            data = load_jsonc(path)
        except (ValueError, OSError) as e:
            raise ModelError(f"could not read {path}: {e}")
        if not data.get("providers"):
            raise ModelError(f"{path} has no 'providers'")
        providers = {n: _normalise_provider(n, p) for n, p in data["providers"].items()}
        default = data.get("default") or _first_ref(providers)
        return {"default": default, "providers": providers, "source": str(path)}

    base_url = OPENAI_BASE_URL.strip()
    api_key = OPENAI_API_KEY.strip()
    name = OPENAI_MODEL.strip() or "default"
    if not base_url or not api_key:
        return {"default": None, "providers": {}, "source": "env(missing)"}
    ptype = _infer_provider_type(base_url)
    providers = {ptype: _normalise_provider(ptype, {
        "provider_type": ptype, "base_url": base_url, "api_key": api_key, "models": [name]})}
    return {"default": f"{ptype}/{name}", "providers": providers, "source": "env"}


def _first_ref(providers: dict) -> str:
    pname = next(iter(providers))
    mname = next(iter(providers[pname]["models"]))
    return f"{pname}/{mname}"


def _spec(pname: str, mname: str, prov: dict) -> dict:
    m = prov["models"][mname]
    info = dict(m.get("info") or {})
    spec = {
        "ref": f"{pname}/{mname}",
        "provider": pname,
        "name": mname,
        "api_model": m.get("model_id") or mname,
        "provider_type": prov["provider_type"],
        "base_url": prov["base_url"],
        "api_key": prov["api_key"],
        "quirks": m.get("quirks") or {},
        "info": info,
    }
    if info.get("context_window") is not None:
        spec["context_window"] = int(info["context_window"])
    return spec


def resolve(ref: str | None) -> dict:
    """Resolve a `provider/model` ref, a unique bare model name, or None (default) to a spec."""
    reg = load_registry()
    providers = reg["providers"]
    if not providers:
        raise ModelError(
            "no models configured: create models.json before launching the dashboard")
    ref = ref or reg["default"]
    if not ref:
        raise ModelError("no model specified and no default in the registry")
    if "/" in ref:
        pname, mname = ref.split("/", 1)
        if pname not in providers:
            raise ModelError(f"unknown provider {pname!r}; known: {sorted(providers)}")
        if mname not in providers[pname]["models"]:
            raise ModelError(
                f"provider {pname!r} has no model {mname!r}; "
                f"has: {sorted(providers[pname]['models'])}")
        return _spec(pname, mname, providers[pname])
    # A bare model name must be unique across providers.
    matches = [(p, m) for p, pv in providers.items() for m in pv["models"] if m == ref]
    if not matches:
        raise ModelError(f"unknown model {ref!r}; run `model list` to see options")
    if len(matches) > 1:
        opts = ", ".join(f"{p}/{m}" for p, m in matches)
        raise ModelError(f"model {ref!r} is offered by multiple providers — qualify it: {opts}")
    return _spec(matches[0][0], matches[0][1], providers[matches[0][0]])


def ref_for(base_url: str | None, api_model: str | None) -> str | None:
    """Reverse-lookup the `provider/model` ref of an api model id; base_url disambiguates."""
    if not api_model:
        return None
    try:
        reg = load_registry()
    except ModelError:
        return api_model
    for pname, pv in reg["providers"].items():
        if base_url and pv["base_url"].rstrip("/") != base_url.rstrip("/"):
            continue
        for mname, m in pv["models"].items():
            if (m.get("model_id") or mname) == api_model:
                return f"{pname}/{mname}"
    for pname, pv in reg["providers"].items():
        for mname, m in pv["models"].items():
            if (m.get("model_id") or mname) == api_model:
                return f"{pname}/{mname}"
    return api_model


def _is_default(ref: str, default: str | None) -> bool:
    if not default:
        return False
    if ref == default:
        return True
    # A bare default name matches that model under any provider.
    return "/" not in default and ref.split("/", 1)[1] == default


def list_models() -> dict:
    """Provider-grouped registry view for the UI/CLI (keys redacted)."""
    reg = load_registry()
    providers = []
    for pname, pv in reg["providers"].items():
        models = []
        for mname, m in pv["models"].items():
            ref = f"{pname}/{mname}"
            models.append({
                "name": mname,
                "ref": ref,
                "api_model": m.get("model_id") or mname,
                "info": dict(m.get("info") or {}),
                "context_window": (m.get("info") or {}).get("context_window"),
                "is_default": _is_default(ref, reg["default"]),
            })
        providers.append({
            "provider": pname,
            "provider_type": pv["provider_type"],
            "base_url": pv["base_url"],
            "has_key": bool(pv["api_key"]),
            "model_count": len(models),
            "models": models,
        })
    return {
        "default": reg["default"],
        "provider_count": len(providers),
        "providers": providers,
        "source": reg["source"],
    }
