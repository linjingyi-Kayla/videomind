from __future__ import annotations

import os
from typing import Any, Dict, List, Protocol

import requests


class WebSearchProvider(Protocol):
    def search(self, query: str, limit: int = 5) -> List[Dict[str, str]]:
        ...


def _trim(s: Any, n: int = 280) -> str:
    t = str(s or "").strip()
    if len(t) > n:
        return t[:n].rstrip() + "…"
    return t


class TavilyProvider:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def search(self, query: str, limit: int = 5) -> List[Dict[str, str]]:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self.api_key,
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        out: List[Dict[str, str]] = []
        for item in data.get("results") or []:
            out.append(
                {
                    "title": _trim(item.get("title"), 160),
                    "snippet": _trim(item.get("content") or item.get("snippet")),
                    "url": str(item.get("url") or "").strip(),
                }
            )
            if len(out) >= limit:
                break
        return out


class BraveProvider:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def search(self, query: str, limit: int = 5) -> List[Dict[str, str]]:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": limit},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self.api_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        web = (data.get("web") or {}).get("results") or []
        out: List[Dict[str, str]] = []
        for item in web:
            out.append(
                {
                    "title": _trim(item.get("title"), 160),
                    "snippet": _trim(item.get("description") or item.get("snippet")),
                    "url": str(item.get("url") or "").strip(),
                }
            )
            if len(out) >= limit:
                break
        return out


class SerperProvider:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def search(self, query: str, limit: int = 5) -> List[Dict[str, str]]:
        resp = requests.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": limit},
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        out: List[Dict[str, str]] = []
        for item in data.get("organic") or []:
            out.append(
                {
                    "title": _trim(item.get("title"), 160),
                    "snippet": _trim(item.get("snippet")),
                    "url": str(item.get("link") or item.get("url") or "").strip(),
                }
            )
            if len(out) >= limit:
                break
        return out


_PROVIDERS = {
    "tavily": TavilyProvider,
    "brave": BraveProvider,
    "serper": SerperProvider,
}


def get_provider() -> WebSearchProvider | None:
    key = (os.getenv("WEB_SEARCH_API_KEY") or "").strip()
    if not key:
        return None
    name = (os.getenv("WEB_SEARCH_PROVIDER") or "tavily").strip().lower()
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"unknown_provider:{name}")
    return cls(key)


def web_search(query: str, limit: int = 5) -> Dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"success": False, "error": "empty_query", "results": []}
    try:
        provider = get_provider()
    except ValueError as e:
        return {"success": False, "error": str(e), "results": []}
    if provider is None:
        return {"success": False, "error": "web_search_not_configured", "results": []}

    n = max(1, min(int(limit or 5), 5))
    try:
        results = provider.search(q, limit=n)
    except Exception as e:
        return {"success": False, "error": f"web_search_failed:{e}", "results": []}

    return {
        "success": True,
        "query": q,
        "results": [
            {
                "title": r.get("title") or "",
                "snippet": r.get("snippet") or "",
                "url": r.get("url") or "",
            }
            for r in (results or [])[:n]
        ],
    }
