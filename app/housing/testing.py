from __future__ import annotations

import json
from pathlib import Path

from app.housing.fetch import FetchError, FetchResponse


class FixtureClient:
    """离线回放：url -> 文件内容，接口与 HousingHttpClient.get 保持一致。"""

    def __init__(self, mapping: dict[str, str | bytes], base_dir: Path | None = None) -> None:
        self.mapping = mapping
        self.base_dir = base_dir

    @classmethod
    def from_directory(cls, directory: Path) -> FixtureClient:
        manifest_path = Path(directory) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return cls(manifest, Path(directory))

    async def get(self, url: str, **kwargs) -> FetchResponse:
        key = _match(self.mapping, url)
        if key is None:
            raise FetchError("http_status", f"没有 fixture: {url}")
        value = self.mapping[key]
        if isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
        else:
            candidate = Path(value)
            if self.base_dir and not candidate.is_absolute():
                candidate = self.base_dir / value
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8")
            else:
                text = value
        return FetchResponse(
            url=url,
            status=200,
            text=text,
            headers={"content-type": "application/json" if text.lstrip().startswith("{") else "text/html"},
        )

    async def close(self) -> None:
        return None

    async def get_bytes(self, url: str, **kwargs) -> bytes:
        key = _match(self.mapping, url)
        if key is None:
            raise FetchError("http_status", f"没有 fixture: {url}")
        value = self.mapping[key]
        if isinstance(value, bytes):
            return value
        if self.base_dir:
            path = self.base_dir / value
            if path.is_file():
                return path.read_bytes()
        return value.encode("utf-8")

    async def __aenter__(self) -> FixtureClient:
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()


def _match(mapping: dict[str, str | bytes], url: str) -> str | None:
    if url in mapping:
        return url
    base = url.split("?")[0]
    for key in mapping:
        if key.split("?")[0] == base:
            return key
    return None


def write_manifest(directory: Path, mapping: dict[str, str]) -> Path:
    path = Path(directory) / "manifest.json"
    path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
