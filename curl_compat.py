"""
Compatibility wrapper: makes curl_cffi's AsyncSession look like aiohttp.ClientSession
with built-in aiohttp fallback safeguard for Chrome TLS fingerprinting.
"""

import logging
import asyncio

logger = logging.getLogger(__name__)

class _CurlResponse:
    """Wraps a curl_cffi response to emulate an aiohttp response."""

    def __init__(self, resp):
        self._r = resp

    @property
    def status(self):
        return self._r.status_code

    @property
    def status_code(self):
        return self._r.status_code

    @property
    def url(self):
        return str(self._r.url)

    @property
    def headers(self):
        return self._r.headers

    def json(self, content_type=None):
        try:
            return self._r.json()
        except Exception:
            return {}

    def text(self):
        return self._r.text if hasattr(self._r, 'text') else str(self._r.content)

    def read(self):
        return self._r.content


class _CurlRequest:
    """Async context manager for a single curl_cffi request."""

    def __init__(self, coro):
        self._coro = coro
        self._resp = None

    async def __aenter__(self):
        resp = await self._coro
        self._resp = _CurlResponse(resp)
        return self._resp

    async def __aexit__(self, *args):
        pass


def _extract_timeout(kwargs, default=12):
    """Extract timeout from kwargs; handle aiohttp.ClientTimeout or int/float."""
    t = kwargs.pop("timeout", default)
    if hasattr(t, "total"):
        return t.total or default
    return t or default


def _clean_kwargs(kwargs):
    """Remove aiohttp-specific kwargs that curl_cffi doesn't recognize."""
    kwargs.pop("connector", None)
    return kwargs


class ChromeSession:
    """
    Drop-in replacement for aiohttp.ClientSession using curl_cffi AsyncSession
    with Chrome TLS impersonation and automatic aiohttp fallback safeguard.
    """

    def __init__(self, impersonate="chrome131", timeout=12, proxies=None, **kwargs):
        self.impersonate = impersonate
        self._default_timeout = timeout
        self.proxies = proxies
        self._session = None
        self._use_aiohttp = False
        self._aiohttp_session = None

    async def __aenter__(self):
        try:
            from curl_cffi.requests import AsyncSession
            session_kwargs = {"impersonate": self.impersonate}
            if self.proxies:
                session_kwargs["proxies"] = self.proxies
            self._session = AsyncSession(**session_kwargs)
            await self._session.__aenter__()
        except Exception as e:
            import aiohttp
            logger.warning(f"curl_cffi AsyncSession failed ({e}), falling back to aiohttp ClientSession")
            self._session = None
            self._use_aiohttp = True
            aio_kwargs = {"timeout": aiohttp.ClientTimeout(total=self._default_timeout)}
            if self.proxies and isinstance(self.proxies, dict):
                p_url = self.proxies.get("https") or self.proxies.get("http")
                if p_url:
                    aio_kwargs["proxy"] = p_url
            self._aiohttp_session = aiohttp.ClientSession(**aio_kwargs)
            await self._aiohttp_session.__aenter__()
            return self._aiohttp_session
        return self

    async def __aexit__(self, *args):
        if self._use_aiohttp and self._aiohttp_session:
            await self._aiohttp_session.__aexit__(*args)
        elif self._session is not None:
            try:
                await self._session.__aexit__(*args)
            except Exception:
                pass

    def get(self, url, **kwargs):
        if self._use_aiohttp:
            return self._aiohttp_session.get(url, **kwargs)
        timeout = _extract_timeout(kwargs, self._default_timeout)
        _clean_kwargs(kwargs)
        if self.proxies and "proxies" not in kwargs:
            kwargs["proxies"] = self.proxies
        return _CurlRequest(self._session.get(url, timeout=timeout, **kwargs))

    def post(self, url, **kwargs):
        if self._use_aiohttp:
            return self._aiohttp_session.post(url, **kwargs)
        timeout = _extract_timeout(kwargs, self._default_timeout)
        _clean_kwargs(kwargs)
        if self.proxies and "proxies" not in kwargs:
            kwargs["proxies"] = self.proxies
        return _CurlRequest(self._session.post(url, timeout=timeout, **kwargs))
