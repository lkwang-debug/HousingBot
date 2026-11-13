from __future__ import annotations

import asyncio
import logging
import random
import ssl
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from app.housing.config import HousingSettings, get_housing_settings

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class FetchResponse:
    url: str
    status: int
    text: str = ""
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    elapsed_ms: int = 0
    json_data: dict | None = None
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""


def _decode(response: httpx.Response) -> str:
    encoding = response.encoding
    candidates = [encoding] if encoding and encoding.lower() != "iso-8859-1" else []
    candidates += ["utf-8", "gb18030"]
    for candidate in candidates:
        try:
            return response.content.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return response.content.decode("utf-8", errors="replace")


class HousingHttpClient:
    """带重试、同 host 串行和全局限速的抓取客户端。"""

    def __init__(
        self,
        settings: HousingSettings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_housing_settings()
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings_timeout(self.settings), connect=10),
            follow_redirects=True,
            verify=_ssl_context(),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            },
        )
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(self.settings.http_concurrency)
        self._host_locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> HousingHttpClient:
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    def _host_lock(self, url: str) -> asyncio.Lock:
        host = urlsplit(url).netloc
        lock = self._host_locks.get(host)
        if lock is None:
            lock = asyncio.Lock()
            self._host_locks[host] = lock
        return lock

    async def _polite_wait(self) -> None:
        delay = self.settings.request_delay_ms / 1000 + random.uniform(0, 0.3)
        await asyncio.sleep(delay)

    async def get(
        self,
        url: str,
        *,
        referer: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        retries: int = 3,
        params: dict | None = None,
        binary: bool = False,
    ) -> FetchResponse:
        headers: dict[str, str] = {}
        if referer:
            headers["Referer"] = referer
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        async with self._host_lock(url):
            async with self._semaphore:
                return await self._get_with_retry(
                    url, headers=headers, retries=retries, params=params, binary=binary
                )

    async def get_bytes(
        self, url: str, *, referer: str | None = None, max_bytes: int = 25 * 1024 * 1024
    ) -> bytes:
        """附件下载：只取二进制，不按 HTML 解码，且限制体积。"""
        response = await self.get(url, referer=referer, binary=True)
        if len(response.content) > max_bytes:
            raise FetchError("too_large", f"附件超过 {max_bytes // 1024 // 1024}MB: {url}")
        return response.content

    async def _get_with_retry(
        self,
        url: str,
        *,
        headers: dict[str, str],
        retries: int,
        params: dict | None,
        binary: bool = False,
    ) -> FetchResponse:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            if attempt:
                await asyncio.sleep(2 ** (attempt - 1) + random.uniform(0, 0.3))
            await self._polite_wait()
            loop = asyncio.get_running_loop()
            started = loop.time()
            try:
                response = await self.client.get(url, headers=headers, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = FetchError("transport", f"{type(exc).__name__}: {exc}")
                continue
            elapsed_ms = int((loop.time() - started) * 1000)
            if response.status_code == 304:
                return FetchResponse(
                    url=str(response.url),
                    status=304,
                    not_modified=True,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    elapsed_ms=elapsed_ms,
                )
            if response.status_code in RETRY_STATUS:
                last_error = FetchError("http_status", f"HTTP {response.status_code} {url}")
                retry_after = response.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    await asyncio.sleep(min(int(retry_after), 30))
                continue
            if response.status_code >= 400:
                raise FetchError("http_status", f"HTTP {response.status_code} {url}")
            text = "" if binary else _decode(response)
            return FetchResponse(
                url=str(response.url),
                status=response.status_code,
                text=text,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                elapsed_ms=elapsed_ms,
                headers=dict(response.headers),
                content=response.content if binary else b"",
            )
        assert last_error is not None
        raise last_error

    async def get_json(
        self, url: str, *, referer: str | None = None, params: dict | None = None
    ) -> dict:
        response = await self.get(url, referer=referer, params=params)
        try:
            import json

            return json.loads(response.text)
        except ValueError as exc:
            raise FetchError("bad_json", f"响应不是合法 JSON: {url}") from exc


def settings_timeout(settings: HousingSettings) -> float:
    return settings.timeout_seconds


def _ssl_context() -> ssl.SSLContext:
    """深圳政府站会下发非常规 EC 曲线，Python 3.10 默认握手报 BAD_ECPOINT。

    固定 prime256v1 可正常完成握手，仍然完整校验证书，不做 verify=False。
    """
    context = ssl.create_default_context()
    try:
        context.set_ecdh_curve("prime256v1")
    except (ValueError, ssl.SSLError):
        pass
    return context
