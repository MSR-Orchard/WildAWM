"""Free and paid web-search providers with process-safe coordination."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Protocol

import requests


class SearchProvider(Protocol):
    """One normalized search provider."""

    name: str

    def search(
        self,
        query: str,
        max_results: int,
        timeout_seconds: float,
    ) -> list[dict[str, str]]: ...


def _is_chinese(query: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", query))


def _normalized_result(
    *,
    title: Any,
    link: Any,
    snippet: Any,
    date: Any,
    query: str,
) -> dict[str, str] | None:
    resolved_link = str(link or "").strip()
    if not resolved_link:
        return None
    return {
        "title": str(title or "").strip(),
        "link": resolved_link,
        "snippet": str(snippet or "").strip(),
        "date": str(date or "").strip(),
        "query": query,
    }


class DDGSSearchProvider:
    """Search through the MIT-licensed DDGS metasearch client."""

    name = "ddgs"

    def __init__(
        self,
        *,
        client_factory: Callable[[float], Any] | None = None,
    ) -> None:
        self._client_factory = client_factory

    def search(
        self,
        query: str,
        max_results: int,
        timeout_seconds: float,
    ) -> list[dict[str, str]]:
        if self._client_factory is None:
            from ddgs import DDGS

            client = DDGS(timeout=timeout_seconds)
        else:
            client = self._client_factory(timeout_seconds)
        rows = list(client.text(query, max_results=max_results))
        results: list[dict[str, str]] = []
        for row in rows[:max_results]:
            if not isinstance(row, Mapping):
                continue
            normalized = _normalized_result(
                title=row.get("title"),
                link=row.get("href") or row.get("url"),
                snippet=row.get("body") or row.get("snippet"),
                date=row.get("date") or row.get("published"),
                query=query,
            )
            if normalized is not None:
                results.append(normalized)
        return results


class SearXNGSearchProvider:
    """Search a loopback or user-supplied SearXNG JSON endpoint."""

    name = "searxng"

    def __init__(
        self,
        *,
        base_url: str,
        get: Callable[..., Any] = requests.get,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        if not self._base_url:
            raise ValueError("SEARXNG_URL cannot be empty")
        self._get = get

    def search(
        self,
        query: str,
        max_results: int,
        timeout_seconds: float,
    ) -> list[dict[str, str]]:
        response = self._get(
            f"{self._base_url}/search",
            params={
                "q": query,
                "format": "json",
                "language": "zh-CN" if _is_chinese(query) else "en-US",
            },
            headers={"X-Real-IP": "127.0.0.1"},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise TypeError("SearXNG response must be a JSON object")
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            raise TypeError("SearXNG results must be a list")
        results: list[dict[str, str]] = []
        for row in rows[:max_results]:
            if not isinstance(row, Mapping):
                continue
            normalized = _normalized_result(
                title=row.get("title"),
                link=row.get("url"),
                snippet=row.get("content") or row.get("snippet"),
                date=(
                    row.get("publishedDate")
                    or row.get("published_date")
                    or row.get("date")
                ),
                query=query,
            )
            if normalized is not None:
                results.append(normalized)
        return results


class NovadaSearchProvider:
    """Compatibility provider for the original paid Novada Google endpoint."""

    name = "novada"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        get: Callable[..., Any] = requests.get,
    ) -> None:
        if not api_key or api_key == "YOUR_API_KEY":
            raise ValueError("SERP_DEV_KEY is required for the novada backend")
        self._base_url = base_url
        self._api_key = api_key
        self._get = get

    def search(
        self,
        query: str,
        max_results: int,
        timeout_seconds: float,
    ) -> list[dict[str, str]]:
        response = self._get(
            self._base_url,
            params={
                "engine": "google",
                "api_key": self._api_key,
                "q": query,
                "num": str(max_results),
                "hl": "zh" if _is_chinese(query) else "en",
                "gl": "cn" if _is_chinese(query) else "us",
                "start": "1",
                "fetch_mode": "static",
                "no_cache": "true",
            },
            timeout=timeout_seconds,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Novada returned HTTP {response.status_code}")
        payload = response.json()
        rows = (
            payload.get("data", {}).get("organic_results", [])
            if isinstance(payload, Mapping)
            else []
        )
        results: list[dict[str, str]] = []
        for row in rows[:max_results]:
            if not isinstance(row, Mapping):
                continue
            normalized = _normalized_result(
                title=row.get("title"),
                link=row.get("url"),
                snippet=row.get("description"),
                date=row.get("date"),
                query=query,
            )
            if normalized is not None:
                results.append(normalized)
        return results


def build_search_providers(
    environment: Mapping[str, str] | None = None,
) -> list[SearchProvider]:
    """Build the configured ordered provider chain."""

    values = os.environ if environment is None else environment
    raw_names = values.get("WEB_SEARCH_BACKENDS", "ddgs,searxng")
    names = [name.strip().lower() for name in raw_names.split(",") if name.strip()]
    if not names:
        raise ValueError("WEB_SEARCH_BACKENDS must name at least one backend")

    providers: list[SearchProvider] = []
    for name in names:
        if name == "ddgs":
            providers.append(DDGSSearchProvider())
        elif name == "searxng":
            providers.append(
                SearXNGSearchProvider(
                    base_url=values.get(
                        "SEARXNG_URL",
                        "http://127.0.0.1:18088",
                    )
                )
            )
        elif name == "novada":
            providers.append(
                NovadaSearchProvider(
                    base_url=values.get(
                        "SERP_API_URL",
                        "https://scraperapi.novada.com/search",
                    ),
                    api_key=values.get("SERP_DEV_KEY", ""),
                )
            )
        else:
            raise ValueError(f"unknown web search backend: {name}")
    return providers


def _safe_error(provider: str, error: BaseException) -> str:
    detail = str(error).replace("\n", " ").replace("\r", " ")[:500]
    detail = re.sub(
        r"(?i)(api[_-]?key|token|secret)=([^&\s]+)",
        r"\1=[REDACTED]",
        detail,
    )
    return f"{provider}: {type(error).__name__}: {detail}"


class ProcessSafeSearchCoordinator:
    """Serialize cache misses and upstream starts across Linux processes."""

    def __init__(
        self,
        *,
        state_dir: Path,
        cache_ttl_seconds: float,
        min_interval_seconds: float,
        max_in_flight: int = 10,
    ) -> None:
        if cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds cannot be negative")
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds cannot be negative")
        if max_in_flight <= 0:
            raise ValueError("max_in_flight must be positive")
        self.state_dir = state_dir
        self.cache_ttl_seconds = float(cache_ttl_seconds)
        self.min_interval_seconds = float(min_interval_seconds)
        self.max_in_flight = max_in_flight
        self._lock_path = self.state_dir / ".search.lock"
        self._rate_path = self.state_dir / ".last_start"

    def _prepare_state_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)

    @contextmanager
    def _locked(self, path: Path):
        self._prepare_state_dir()
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def _provider_slot(self):
        self._prepare_state_dir()
        while True:
            for index in range(self.max_in_flight):
                path = self.state_dir / f".provider-slot-{index:03d}.lock"
                descriptor = os.open(
                    path,
                    os.O_RDWR | os.O_CREAT,
                    0o600,
                )
                os.fchmod(descriptor, 0o600)
                try:
                    fcntl.flock(
                        descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except BlockingIOError:
                    os.close(descriptor)
                    continue
                try:
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
                return
            time.sleep(0.01)

    def _cache_path(self, query: str, max_results: int) -> Path:
        raw = json.dumps(
            {"query": query, "max_results": max_results},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return self.state_dir / f"cache-{digest}.json"

    def _cache_lock_path(self, cache_path: Path) -> Path:
        return self.state_dir / f".{cache_path.stem}.lock"

    def _read_cache(
        self,
        path: Path,
    ) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            created_at = float(value["created_at"])
            backend = value["backend"]
            output = value["output"]
            if (
                time.time() - created_at > self.cache_ttl_seconds
                or not isinstance(backend, str)
                or not isinstance(output, list)
                or not output
            ):
                path.unlink(missing_ok=True)
                return None
            return {
                "status": 200,
                "output": output,
                "backend": backend,
                "cached": True,
                "errors": [],
            }
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return None

    def _atomic_write(self, path: Path, value: object) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-",
            suffix=".tmp",
            dir=self.state_dir,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _atomic_write_text(self, path: Path, value: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-",
            suffix=".tmp",
            dir=self.state_dir,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _wait_for_slot(self) -> None:
        now = time.monotonic()
        last_started: float | None = None
        try:
            last_started = float(self._rate_path.read_text(encoding="ascii"))
        except (OSError, ValueError):
            pass
        if last_started is not None and last_started <= now:
            delay = last_started + self.min_interval_seconds - now
            if delay > 0:
                time.sleep(delay)
        self._atomic_write_text(
            self._rate_path,
            f"{time.monotonic():.9f}",
        )

    @staticmethod
    def _validate_results(
        value: object,
        query: str,
        max_results: int,
    ) -> list[dict[str, str]]:
        if not isinstance(value, list):
            raise TypeError("provider results must be a list")
        results: list[dict[str, str]] = []
        for item in value[:max_results]:
            if not isinstance(item, Mapping):
                continue
            normalized = _normalized_result(
                title=item.get("title"),
                link=item.get("link"),
                snippet=item.get("snippet"),
                date=item.get("date"),
                query=query,
            )
            if normalized is not None:
                results.append(normalized)
        return results

    def search(
        self,
        query: str,
        max_results: int,
        providers: Sequence[SearchProvider],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query cannot be empty")
        if not 1 <= max_results <= 10:
            raise ValueError("max_results must be between 1 and 10")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not providers:
            raise ValueError("at least one search provider is required")

        cache_path = self._cache_path(query, max_results)
        with self._locked(self._cache_lock_path(cache_path)):
            cached = self._read_cache(cache_path)
            if cached is not None:
                return cached

            errors: list[str] = []
            for provider in providers:
                try:
                    with self._provider_slot():
                        with self._locked(self._lock_path):
                            self._wait_for_slot()
                        raw_results = provider.search(
                            query,
                            max_results,
                            timeout_seconds,
                        )
                    results = self._validate_results(
                        raw_results,
                        query,
                        max_results,
                    )
                except Exception as error:
                    errors.append(_safe_error(provider.name, error))
                    continue
                if not results:
                    errors.append(f"{provider.name}: empty results")
                    continue

                value = {
                    "created_at": time.time(),
                    "backend": provider.name,
                    "output": results,
                }
                self._atomic_write(cache_path, value)
                return {
                    "status": 200,
                    "output": results,
                    "backend": provider.name,
                    "cached": False,
                    "errors": errors,
                }

            return {
                "status": -1,
                "output": [],
                "backend": None,
                "cached": False,
                "errors": errors,
            }


def search_web(
    query: str,
    max_results: int = 10,
    timeout_seconds: float = 20.0,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Search with the environment-configured free provider chain."""

    values = os.environ if environment is None else environment
    ttl_raw = values.get("WEB_SEARCH_CACHE_TTL_SECONDS")
    if ttl_raw is None:
        ttl_seconds = float(values.get("CACHE_TTL", "24")) * 3600.0
    else:
        ttl_seconds = float(ttl_raw)
    coordinator = ProcessSafeSearchCoordinator(
        state_dir=Path(
            values.get(
                "WEB_SEARCH_STATE_DIR",
                str(Path(__file__).resolve().parents[2] / "runtime" / "web-search"),
            )
        ),
        cache_ttl_seconds=ttl_seconds,
        min_interval_seconds=float(
            values.get("WEB_SEARCH_MIN_INTERVAL_SECONDS", "0.0")
        ),
        max_in_flight=int(
            values.get("WEB_SEARCH_MAX_IN_FLIGHT", "10")
        ),
    )
    return coordinator.search(
        query,
        max_results,
        build_search_providers(values),
        timeout_seconds,
    )
