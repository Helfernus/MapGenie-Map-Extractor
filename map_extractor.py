from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import certifi

SYSTEM_TRUST_ENABLED = False
SYSTEM_TRUST_BACKEND = "requests/certifi"
SYSTEM_TRUST_ERROR: str | None = None
_TEMP_CA_BUNDLE: Path | None = None

try:
    import truststore # type: ignore[import-not-found]

    truststore.inject_into_ssl()
    SYSTEM_TRUST_ENABLED = True
    SYSTEM_TRUST_BACKEND = "OS trust store (truststore)"
except Exception as exc: # host-dependent
    SYSTEM_TRUST_ERROR = str(exc)


def _build_windows_system_ca_bundle() -> Path | None:
    """Merge certifi with native Windows roots when truststore is unavailable."""
    if os.name != "nt" or not hasattr(ssl, "enum_certificates"):
        return None
    try:
        blocks = [Path(certifi.where()).read_text(encoding="ascii")]
        seen: set[bytes] = set()
        for store in ("ROOT", "CA"):
            for der, encoding, _ in ssl.enum_certificates(store): # type: ignore[attr-defined]
                if encoding == "x509_asn" and der not in seen:
                    seen.add(der)
                    blocks.append(ssl.DER_cert_to_PEM_cert(der))
        if not seen:
            return None
        path = Path(tempfile.gettempdir()) / f"mapgenie_system_ca_{os.getpid()}.pem"
        path.write_text("\n".join(blocks), encoding="ascii")
        return path
    except Exception:
        return None


if not SYSTEM_TRUST_ENABLED:
    TEMP_CA_BUNDLE = _build_windows_system_ca_bundle()
    if _TEMP_CA_BUNDLE:
        SYSTEM_TRUST_ENABLED = True
        SYSTEM_TRUST_BACKEND = "Windows ROOT/CA + certifi"
        atexit.register(lambda: _TEMP_CA_BUNDLE and _TEMP_CA_BUNDLE.unlink(missing_ok=True))

DEFAULT_TLS_VERIFY: bool | str = str(_TEMP_CA_BUNDLE) if _TEMP_CA_BUNDLE else True

import requests
import urllib3
from PIL import Image
from urllib3.exceptions import InsecureRequestWarning

CURL_CFFI_AVAILABLE = False
CURL_CFFI_IMPORT_ERROR: str | None = None
curl_requests = CurlOpt = CurlRequestException = None
try:
    from curl_cffi import CurlOpt as _CurlOpt # type: ignore[import-not-found]
    from curl_cffi import requests as _curl_requests # type: ignore[import-not-found]
    from curl_cffi.requests.exceptions import RequestException as _CurlRequestException # type: ignore[import-not-found]

    CurlOpt, curl_requests, CurlRequestException = _CurlOpt, _curl_requests, _CurlRequestException
    CURL_CFFI_AVAILABLE = True
except Exception as exc: # optional dependency in tests/dev
    CURL_CFFI_IMPORT_ERROR = str(exc)

_TILE_REQUEST_EXCEPTIONS: tuple[type[BaseException], ... ] = (requests.RequestException,)
if CurlRequestException is not None:
    _TILE_REQUEST_EXCEPTIONS += (CurlRequestException,)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
DEFAULT_TILES_BASE_URL = "https://tiles.mapgenie.io"
DEFAULT_API_BASE_URL = "https://mapgenie.io/api/v1"
APP_VERSION = "1.0"
ProgressCallback = Callable[[str, int, int], None]


def format_elapsed(seconds: float) -> str:
    minutes, seconds = divmod(max(0, round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

def open_folder(path: Path) -> None:
    path = path.resolve()
    if os.name == "nt":
        os.startfile(path) # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


@dataclass(frozen=True)
class TileBounds:
    x_min: int
    x_max: int
    y_min: int
    y_max: int

    @property
    def columns(self) -> int:
        return self.x_max - self.x_min + 1

    @property
    def rows(self) -> int:
        return self.y_max - self.y_min + 1

    @property
    def count(self) -> int:
        return self.columns * self.rows


@dataclass
class TileSetInfo:
    index: int
    name: str
    pattern: str
    extension: str
    min_zoom: int
    max_zoom: int
    bounds: dict[int, TileBounds]
    raw: dict

    def bounds_for(self, zoom: int) -> TileBounds:
        if zoom in self.bounds:
            return self.bounds[zoom]
        if not self.bounds:
            raise ValueError(f"Tile set {self.index} exposes no per-zoom bounds")
        lower = [z for z in self.bounds if z < zoom]
        source_zoom = max(lower) if lower else min(z for z in self.bounds if z > zoom)
        source = self.bounds[source_zoom]
        factor = 2 ** abs(zoom - source_zoom)
        if source_zoom < zoom:
            return TileBounds(
                source.x_min * factor,
                (source.x_max + 1) * factor - 1,
                source.y_min * factor,
                (source.y_max + 1) * factor - 1,
            )
        return TileBounds(
            source.x_min // factor,
            source.x_max // factor,
            source.y_min // factor,
            source.y_max // factor,
        )


@dataclass
class MapInfo:
    game_slug: str
    map_slug: str
    title: str
    map_id: int | None
    tiles_base_url: str
    tile_sets: list[TileSetInfo]
    source_url: str

class MapGenieError(RuntimeError):
    pass

def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _clean_host(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        raw = parsed.hostname or parsed.netloc
    raw = raw.split(":", 1)[0]
    return raw[4:] if raw.startswith("www.") else raw


class MapGenieClient:
    def __init__(self, timeout: int = 30, verify: bool | str | None = None):
        self.timeout = timeout
        self.verify: bool | str = DEFAULT_TLS_VERIFY if verify is None else verify
        self.session = requests.Session()
        self.session.verify = self.verify
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        if self.verify is False:
            urllib3.disable_warnings(InsecureRequestWarning)
        self ._games_cache: list[dict] | None = None

    @staticmethod
    def tls_status() -> str:
        if SYSTEM_TRUST_ENABLED:
            return f"TLS trust: {SYSTEM_TRUST_BACKEND}"
        detail = f" ({SYSTEM_TRUST_ERROR})" if SYSTEM_TRUST_ERROR else ""
        return f"TLS trust: Requests/certifi only{detail}"

    def _get(self, url: str, ** kwargs):
        try:
            return self.session.get(url, timeout=kwargs.pop("timeout", self.timeout), ** kwargs)
        except requests.exceptions. SSLError as exc:
            mode = (
                "certificate verification is disabled"
                if self.verify is False
                else f"verification source: {self.verify}"
                if isinstance(self.verify, str)
                else self.tls_status()
            )
            raise MapGenieError(
                f"TLS certificate verification failed while connecting to {url}.\n{mode}.\n"
                "Use the Windows/system trust store, --ca-bundle <file.pem>, or --insecure only as a last resort."
            ) from exc

    @staticmethod
    def source_host(url: str) -> str:
        parsed = urlparse(url.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise MapGenieError("Expected an http(s) map page URL")
        return (parsed.hostname or parsed.netloc).lower()

    @staticmethod
    def parse_map_url(url: str) -> tuple[str, str]:
        parsed = urlparse(url.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise MapGenieError("Expected an http(s) map page URL")
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 3 and parts[1].lower() == "maps":
            return parts[0], parts[2]
        host = _clean_host(parsed.hostname or parsed.netloc)
        if host == "rdr2map.com":
            return "rdr2", "world"
        slug = lambda s: re.sub(r"[^a-z0-9_-]+", "-", s.lower()).strip("-") # noqa: E731
        return slug(host.split(".", 1)[0]) or "map", slug(parts[-1] if parts else "world") or "world"

    @classmethod
    def _extract_named_json(cls, html: str, variable_name: str) -> dict | list:
        patterns = [rf"{re.escape(variable_name)}\s *= "]
        if variable_name == "window.mapData":
            patterns += [r"window\[['\"]mapData['\"]\]\s *= ", r"( ?: const|let|var)\s+mapData\s *= "]
        for pattern in patterns:
            if match := re.search(pattern, html):
                return cls ._extract_assigned_json(html, match.group(0))
        raise MapGenieError(f"Could not find a JSON assignment for {variable_name!r} in the page HTML")

    @staticmethod
    def _extract_assigned_json(html: str, marker: str) -> dict | list:
        pos = html.find(marker)
        if pos < 0:
            raise MapGenieError(f"Could not find {marker!r} in the page HTML")
        pos += len(marker)
        while pos < len(html) and html[pos] not in "[{":
            pos += 1
        if pos == len(html):
            raise MapGenieError(f"Found {marker!r}, but no JSON object followed it")
        opening, closing = ("{", "}") if html[pos] == "{" else ("[", "]")
        depth = 0
        in_string = escaped = False
        for i, ch in enumerate(html[pos:], pos):
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == opening:
                depth += 1
            elif ch == closing:
                depth -= 1
                if not depth:
                    try:
                        return json.loads(html[pos : i + 1])
                    except json.JSONDecodeError as exc:
                        raise MapGenieError(f"Embedded {marker!r} value was not valid JSON: {exc}") from exc
        raise MapGenieError(f"Unterminated JSON value after {marker!r}")

    def fetch_games(self, force: bool = False) -> list[dict]:
        if self._games_cache is not None and not force:
            return self._games_cache
        response = self._get(f"{DEFAULT_API_BASE_URL}/games")
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            data = next((data[k] for k in ("data", "games", "results") if isinstance(data.get(k), list)), data)
        if not isinstance(data, list):
            raise MapGenieError("Unexpected response from MapGenie games API")
        self ._games_cache = [g for g in data if isinstance(g, dict)]
        return self._games_cache

    @staticmethod
    def _slug_similarity(a: str, b: str) -> tuple[int, int]:
        tokens = lambda s: {t for t in re.split(r"[^a-z0-9]+", s.lower()) if t} # noqa: E731
        ta, tb = tokens(a), tokens(b)
        return len(ta & tb), -len(ta ^ tb)

    def _fetch_full(self, kind: str, item_id: int | str) -> dict | None:
        try:
            response = self ._get(f"{DEFAULT_API_BASE_URL}/{kind}/{item_id}/full")
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else None
        except (requests.RequestException, ValueError, MapGenieError):
            return None

    def fetch_game_full(self, game_id: int | str) -> dict | None:
        return self ._fetch_full("games", game_id)

    def fetch_map_full(self, map_id: int | str) -> dict | None:
        return self ._fetch_full("maps", map_id)

    _normalise_domain = staticmethod(_clean_host)

    def find_game(self, game_slug: str, map_slug: str | None = None, source_host: str | None = None) -> dict | None:
        try:
            games = self.fetch_games()
        except (requests.RequestException, ValueError, MapGenieError):
            return None

        def has_map(game: dict) -> bool:
            return bool(map_slug) and any(
                isinstance(m, dict) and m.get("slug") == map_slug for m in game.get("maps") or []
            )

        if source_host:
            matches = [g for g in games if _clean_host(g.get("domain")) == _clean_host(source_host)]
            if len(matches) == 1:
                return matches[0]
            if matches:
                exact = next((g for g in matches if g.get("slug") == game_slug), None)
                if exact:
                    return exact
                anchored = [g for g in matches if has_map(g)]
                if len(anchored) == 1:
                        return anchored[0]

        if exact := next((g for g in games if g.get("slug") == game_slug), None):
            return exact
        candidates = [g for g in games if has_map(g)]
        if len(candidates) <= 1:
            return candidates[0] if candidates else None
        return max(candidates, key=lambda g: self._slug_similarity(game_slug, str(g.get("slug") or "")))

    def list_game_maps(
        self, game_slug: str, anchor_map_slug: str | None = None, source_host: str | None = None
    ) -> list[dict]:
        game = self.find_game(game_slug, anchor_map_slug, source_host)
        maps = [] if not game else [
            m for m in game.get("maps") or [] if isinstance(m, dict) and m.get("enabled", True) and m.get("slug")
        ]
        return sorted(maps, key=lambda m: (m.get("order", 0), m.get("title", "")))
    
    @staticmethod
    def _normalise_bounds(raw: Any) -> dict[int, TileBounds]:
        if isinstance(raw, list):
            items: Iterable[tuple[Any, Any]] = enumerate(raw)
        elif isinstance(raw, dict):
            items = raw.items()
        else:
            return {}
        result: dict[int, TileBounds] = {}
        for zoom, item in items:
            try:
                x, y = item["x"], item["y"]
                result[int(zoom)] = TileBounds(int(x["min"]), int(x["max"]), int(y["min"]), int(y["max"]))
            except (KeyError, TypeError, ValueError):
                pass
        return result

    @staticmethod
    def _tile_pattern(raw: dict) -> str | None:
        value = raw.get("pattern") or raw.get("url") or raw.get("tile_url") or raw.get("tileUrl")
        if not value and isinstance(raw.get("tiles"), list) and raw["tiles"]:
            value = raw["tiles"][0]
        return str(value) if value else None

    @staticmethod
    def _extension_from_pattern(pattern: str, raw: dict) -> str:
        if explicit := raw.get("extension") or raw.get("ext"):
            return str(explicit).lower().lstrip(".")
        path = urlparse(pattern).path if "://" in pattern else pattern.split("?", 1)[0]
        match = re.search(r"].([A-Za-z0-9]+)$", path)
        return match.group(1).lower() if match else "png"

    @classmethod
    def _find_tile_sets_payload(cls, payload: object, depth: int = 0) -> tuple[list[dict], dict]:
        if depth > 8:
            return [], 0
        if isinstance(payload, dict):
            for key in ("tile_sets", "tileSets"):
                if isinstance(payload.get(key), list):
                    rows = [v for v in payload[key] if isinstance(v, dict)]
                    if rows:
                        return rows, payload
            if isinstance(payload.get("sources"), dict):
                rows = []
                for source_id, source in payload["sources"].items():
                    if isinstance(source, dict) and isinstance(source.get("tiles"), list) and source["tiles"]:
                        row = dict(source)
                        row.setdefault("name", str(source_id))
                        row.setdefault("pattern", source["tiles"][0])
                        rows. append(row)
                if rows:
                    return rows, payload
            preferred = [payload[k] for k in ("mapConfig", "map_config", "config", "map", "data", "style") if k in payload]
            preferred_ids = {id(v) for v in preferred}
            values = preferred + [v for v in payload.values() if isinstance(v, (dict, list)) and id(v) not in preferred_ids]
        elif isinstance(payload, list):
            values = payload
        else:
            return [], {}
        for value in values:
            rows, container = cls ._find_tile_sets_payload(value, depth + 1)
            if rows:
                return rows, container
        return [], {}

    @staticmethod
    def _infer_pattern_slugs(pattern: str) -> tuple[str | None, str | None]:
        clean = pattern.replace("\\/","/")
        if clean.startswith("//"):
            clean = "https:" + clean
        parts = [p for p in (urlparse(clean).path if "://" in clean else clean.split("?", 1)[0]).split("/") if p]
        if parts and parts[0].lower() == "games":
            parts.pop(0)
        if len(parts) < 2 or any("{" in p or "}"in p for p in parts[:2]):
            return None, None
        return parts[0], parts[1]

    @staticmethod
    def _config_tiles_base_url(*payloads: object) -> str | None:
        keys = ("tiles_base_url", "tilesBaseUrl", "tile_base_url", "tileBaseUrl")
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            for source in (payload, payload.get("config")):
                if not isinstance(source, dict):
                    continue
                for key in keys:
                    value = source.get(key)
                    if isinstance(value, str) and value.startswith(("http://", "https://")):
                        return value.rstrip("/")
        return None

    @staticmethod
    def _map_candidate(game: dict | None, map_slug: str | None, map_id: int | None) -> dict | None:
        if not isinstance(game, dict):
            return None
        maps = [m for m in game.get("maps") or [] if isinstance(m, dict)]
        if map_id is not None and (match := next((m for m in maps if str(m.get("id")) == str(map_id)), None)):
            return match
        if map_slug and (match := next((m for m in maps if m.get("slug") == map_slug), None)):
            return match
        default_id = next((game[k] for k in ("default_map_id", "defaultMapId", "map_id", "mapId") if game.get(k) is not None), None)
        if default_id is not None and (match := next((m for m in maps if str(m.get("id")) == str(default_id)), None)):
            return match
        enabled = [m for m in maps if m.get("enabled", True)]
        return enabled[0] if len(enabled) == 1 else None

    @classmethod
    def _tile_set_info(cls, raw: dict, index: int) -> TileSetInfo | None:
        pattern = cls ._tile_pattern(raw)
        if not pattern:
            return None
        min_zoom = int(raw.get("min_zoom", raw.get("minZoom", raw.get("minzoom", 0))))
        max_zoom = int(raw.get("max_zoom", raw.get("maxZoom", raw.get("maxzoom", min_zoom))))
        name = raw.get("name") or raw.get("title") or raw.get("label") or raw.get("id") or f"tileset-{index}"
        bounds = raw.get("bounds") or raw.get("tile_bounds") or raw.get("tileBounds")
        return TileSetInfo(
            index,
            str(name),
            str(pattern).replace("\\/", "/"),
            cls ._extension_from_pattern(pattern, raw),
            min_zoom,
            max_zoom,
            cls ._normalise_bounds(bounds),
            raw,
        )

    def inspect_map(self, url: str) -> MapInfo:
        game_hint, map_hint = self.parse_map_url(url)
        source_host = self.source_host(url)
        canonical = _clean_host(source_host) == "mapgenie.io" and "/maps/" in urlparse(url).path
        response = self._get(url)
        response.raise_for_status()
        try:
            extracted = self._extract_named_json(response.text, "window.mapData")
            map_data = extracted if isinstance(extracted, dict) else None
        except MapGenieError:
            map_data = None

        embedded = map_data.get("map") if isinstance(map_data, dict) and isinstance(map_data.get("map"), dict) else {}
        embedded_id = _int_or_none(embedded.get("id"))
        map_slug_hint = embedded.get("slug") or map_hint
        game = self.find_game(game_hint, map_slug_hint, source_host)
        game_full = self.fetch_game_full(game["id"]) if game and game.get("id") is not None else None
        effective_game = game_full or game
        candidate = self._map_candidate(effective_game, map_slug_hint, embedded_id)
        if candidate is None and game_full:
            candidate = self ._map_candidate(game, map_slug_hint, embedded_id)

        rows, config = self ._find_tile_sets_payload(map_data) if map_data else ([], {})
        api_map = None
        if not rows:
            api_id = embedded_id if embedded_id is not None else _int_or_none((candidate or {}).get("id"))
            if api_id is not None and (api_map := self.fetch_map_full(api_id)):
                rows, config = self ._find_tile_sets_payload(api_map)
        if not rows and effective_game:
            rows, config = self ._find_tile_sets_payload(effective_game)
        if not rows:
            hint ="" if canonical else f" Branded-domain game lookup was {'successful' if game else 'unsuccessful'}."
            raise MapGenieError("Could not find MapGenie tile-set configuration in the page or public full-map API." + hint)

        first_pattern = next((p for raw in rows if (p := self._tile_pattern(raw))), None)
        pattern_game, pattern_map = self._infer_pattern_slugs(first_pattern) if first_pattern else (None, None)
        if canonical:
            game_slug, map_slug = game_hint, map_hint
        else:
            game_slug = str(pattern_game or (effective_game or {}).get("slug") or game_hint)
            map_slug = str(embedded.get("slug") or pattern_map or (candidate or {}).get("slug") or map_hint)

        ids = (embedded_id, _int_or_none((candidate or {}).get("id")), _int_or_none((api_map or {}).get("id")))
        map_id = next((value for value in ids if value is not None), None)
        title = str(embedded.get("title") or (candidate or {}).get("title") or (api_map or {}).get("title") or map_slug)
        base = self ._config_tiles_base_url(config, map_data, api_map, effective_game, game) or DEFAULT_TILES_BASE_URL
        if first_pattern and first_pattern.startswith(("http://", "https://")):
            parsed = urlparse(first_pattern)
            base = f"{parsed.scheme}://{parsed.netloc}"
        tile_sets = [ts for i, raw in enumerate(rows) if (ts := self._tile_set_info(raw, i))]
        if not tile_sets:
            raise MapGenieError("Tile-set configuration contained no usable URL patterns")
        return MapInfo(game_slug, map_slug, title, map_id, str(base), tile_sets, url)

    #Checkpoint 1

    @staticmethod
    def _dedupe_urls(urls: Iterable[str]) -> list[str]:
        cleaned = (u.split("#", 1)[0].split("?", 1)[0].rstrip("/") for u in urls if u)
        return list(dict. fromkeys(u for u in cleaned if u))

    def _discover_map_urls_from_page(self, map_url: str) -> list[str]:
        game_slug, _= self.parse_map_url(map_url)
        response = self ._get(map_url)
        response.raise_for_status()
        pattern = re.compile(
            rf"( ?: https ?: //( ?: www\.)?mapgenie\.io)?/{re.escape(game_slug)}/maps/([a-zA-Z0-9_-]+)", re.I
        )
        urls = [map_url] + [
            f"https://mapgenie.io/{game_slug}/maps/{m.group(1)}"
            for m in pattern.finditer(response.text.replace("\\/", "/"))
        ]
        return self ._dedupe_urls(urls)

    def map_urls_for_game(self, map_url: str) -> list[str]:
        game_slug, map_slug = self.parse_map_url(map_url)
        host = self.source_host(map_url)
        if _clean_host(host) != "mapgenie.io":
            return [map_url]
        maps = self.list_game_maps(game_slug, map_slug, host)
        if maps:
            return self ._dedupe_urls([map_url]+[f"https://mapgenie.io/{game_slug}/maps/{m['slug']}" for m in maps])
        try:
            return self._discover_map_urls_from_page(map_url)
        except requests.RequestException:
            return [map_url]
        

def tiles_root(base_url: str) -> str:
    root = base_url.rstrip("/")
    return root if root.endswith("/games") else root + "/games"


def make_tile_url(map_info: MapInfo, tile_set: TileSetInfo, z: int, x: int, y: int) -> str:
    pattern = tile_set.pattern.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y)).replace("\\/", "/")
    if pattern.startswith("//"):
        return "https:" + pattern
    if pattern.startswith(("http://", "https://")):
        return pattern
    base = map_info.tiles_base_url.rstrip("/")
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else base
    if pattern.startswith("/games/"):
        return origin.rstrip("/") + pattern
    if pattern.startswith("games/"):
        return origin.rstrip("/") +"/" + pattern
    return f"{tiles_root(base)}/{pattern.lstrip('/')}"


def _browser_referer(source_url: str) -> str:
    parsed = urlparse(source_url.strip())
    if not parsed. scheme or not parsed.netloc:
        return source_url
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"+(f"?{parsed.query}" if parsed.query else "")


def _browser_origin(source_url: str) -> str:
    parsed = urlparse(source_url.strip())
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


def _is_mapgenie_host(hostname: str) -> bool:
    host = hostname. lower(). strip(".")
    return host == "mapgenie. io" or host.endswith(".mapgenie.io")


def _resolve_tile_transport(source_url: str, requested: str = "auto") -> str:
    requested = (requested or "auto").strip().lower()
    if requested not in {"auto", "requests", "chrome"}:
        raise MapGenieError(f"Unknown tile transport: {requested!r}")
    if requested == "auto":
        requested = "requests" if _is_mapgenie_host(urlparse(source_url).hostname or "") else "chrome"
    if requested == "chrome" and not CURL_CFFI_AVAILABLE:
        detail = f" ({CURL_CFFI_IMPORT_ERROR})" if CURL_CFFI_IMPORT_ERROR else ""
        raise MapGenieError(
            "Chrome tile transport requires the 'curl_cffi' package" + detail + ".\n"
            "Run setup windows.bat again or: python -m pip install -U curl_cffi"
        )
    return requested


def _tile_headers(source_url: str, chrome_transport: bool = False) -> dict[str, str]:
    headers = {"Accept-Language": "en-US,en;q=0.9"}
    if not chrome_transport:
        headers.update({"User-Agent": USER_AGENT, "Connection": "keep-alive"})
    host = (urlparse(source_url).hostname or "").lower()
    if source_url:
        headers["Referer"] = _browser_referer(source_url)
    if source_url and not _is_mapgenie_host(host):
        headers.update(
            {
                "Accept": "image/webp,*/*",
                "Origin": _browser_origin(source_url),
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
            }
        )
        if not chrome_transport:
            headers.update(
                {
                    "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                }
            )
    else:
        headers.update(
            {
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Sec-Fetch-Dest": "image",
                "Sec-Fetch-Mode": "no-cors",
            }
        )
        if source_url:
            headers["Sec-Fetch-Site"] = "same-site"
    return headers


def _curl_verify_settings(verify: bool | str) -> tuple[bool, dict[Any, str]]:
    if not isinstance(verify, str):
        return bool(verify), {}
    return (True, {CurlOpt.CAINFO: verify}) if CurlOpt is not None else (True, {})


def _thread_session(
    verify: bool | str = DEFAULT_TLS_VERIFY, source_url: str = "", transport: str = "requests"
) -> Any:
    resolved = _resolve_tile_transport(source_url, transport)
    local = getattr(_thread_session, "local", None) or threading. local()
    _thread_session.local = local
    key = str(verify), source_url, resolved
    if getattr(local, "session_key", None) == key:
        return local.session
    if resolved == "chrome":
        assert curl_requests is not None
        curl_verify, options = _curl_verify_settings(verify)
        session = curl_requests.Session(
            impersonate="chrome",
            default_headers=True,
            headers=_tile_headers(source_url, True),
            verify=curl_verify,
            curl_options=options,
            timeout=30,
            trust_env=True,
        )
    else:
        session = requests.Session()
        session.verify = verify
        session.headers.update(_tile_headers(source_url))
    local.session, local.session_key = session, key
    return session


def _looks_like_image(data: bytes) -> bool:
    return data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")) or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")


def probe_tile(
    map_info: MapInfo,
    tile_set: TileSetInfo,
    z: int,
    x: int,
    y: int,
    verify: bool | str = DEFAULT_TLS_VERIFY,
    transport: str = "auto",
) -> dict:
    resolved = _resolve_tile_transport(map_info.source_url, transport)
    url = make_tile_url(map_info, tile_set, z, x, y)
    try:
        response = _thread_session(verify, map_info.source_url, resolved).get(url, timeout=30)
    except _TILE_REQUEST_EXCEPTIONS as exc:
        raise MapGenieError(f"Tile probe failed using {resolved} transport: {exc}") from exc
    content = bytes(response.content or b"")
    headers = response.headers
    is_image = _looks_like_image(content)
    return {
        "transport": resolved,
        "url": url,
        "status": int(response.status_code),
        "content_type": headers.get("content-type"),
        "content_length": len(content),
        "is_image": is_image,
        "http_version": getattr(response, "http_version", None),
        "access_control_allow_origin": headers.get("access-control-allow-origin"),
        "x_cache": headers.get("x-cache"),
        "server": headers.get("server"),
        "body_preview": "" if is_image else content[:240].decode("utf-8", "replace"),
    }


class RequestPacer:
    """Global request spacing plus shared 403/429 cooldown."""

    def __init__(self, request_delay: float = 0.12):
        self.lock = threading.Lock()
        self.request_delay = max(0.0, float(request_delay))
        self.next_request_at = self.blocked_until = 0.0
        self.strikes = 0

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                target = max(self.next_request_at, self.blocked_until)
                if target <= now:
                    self.next_request_at = now + self.request_delay
                    return
            time.sleep(min(target - now, 1.0))

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        try:
            return max(0.0, float(value.strip())) if value else None
        except ValueError:
            return None

    def penalize(self, status_code: int, retry_after: str | None = None) -> float:
        with self.lock:
            now = time.monotonic()
            if self.blocked_until > now + 0.5:
                return self.blocked_until - now
            self.strikes += 1
            explicit = self._retry_after_seconds(retry_after)
            cooldown = min(300.0, explicit) if explicit is not None else min(
                120.0, (12.0 if status_code == 403 else 8.0) * 2 ** min(self.strikes - 1, 3)
            )
            self.request_delay = min(1.5, max(self.request_delay, 0.18) * 1.35)
            self.blocked_until = self.next_request_at = max(self.blocked_until, now + cooldown)
            return cooldown


def _storage_access_denied(status: int, content_type: str, content: bytes | str) -> bool:
    body = content.encode() if isinstance(content, str) else content
    return status == 403 and "xml" in content_type.lower() and b"<Code>AccessDenied</Code>" in body


def _is_storage_access_denied_response(response: Any) -> bool:
    headers = getattr(response, "headers", {}) or {}
    return _storage_access_denied(
        int(getattr(response, "status_code", 0) or 0),
        str(headers.get("content-type") or headers.get("Content-Type") or ""),
        bytes(getattr(response, "content", b"") or b"")[:1024],
    )


def _probe_points(bounds: TileBounds, limit: int = 5) -> list[tuple[int, int]]:
    x_span, y_span = bounds.x_max - bounds.x_min, bounds.y_max - bounds.y_min
    fractions = ((.5, .5), (.25, .25), (.75, .25), (.25, .75), (.75, .75), (.5, .25), (.5, .75), (.25, .5), (.75, .5))
    points = [
        (bounds.x_min + round(x_span * fx), bounds.y_min + round(y_span * fy)) for fx, fy in fractions
    ]
    return list(dict.fromkeys(points))[: max(1, limit)]


def find_highest_downloadable_zoom(
    map_info: MapInfo,
    tile_set: TileSetInfo,
    verify: bool | str = DEFAULT_TLS_VERIFY,
    transport: str = "auto",
    samples_per_zoom: int = 5,
    progress: ProgressCallback | None = None,
    max_zoom: int | None = None,
) -> tuple[int, list[dict]]:
    """Probe downward from the advertised/configured maximum."""
    top = tile_set.max_zoom if max_zoom is None else min(int(max_zoom), tile_set.max_zoom)
    if top < tile_set.min_zoom:
        raise MapGenieError(f"Zoom z{top} is below tile set {tile_set.index} minimum z{tile_set.min_zoom}")
    diagnostics: list[dict] = []
    total_levels = top - tile_set.min_zoom + 1
    for z in range(top, tile_set.min_zoom - 1, -1):
        bounds = tile_set.bounds_for(z)
        points = _probe_points(bounds, samples_per_zoom)
        level = {"zoom": z, "bounds": asdict(bounds), "probes": [], "accessible": False}
        if progress:
            progress(f"Preflight tile set {tile_set.index}: checking z{z}", top - z, total_levels)
        transport_errors = 0
        for x, y in points:
            try:
                result = probe_tile(map_info, tile_set, z, x, y, verify, transport)
            except MapGenieError as exc:
                transport_errors += 1
                level["probes"].append({"x": x, "y": y, "error": str(exc)})
                continue
            compact = {
                "x": x,
                "y": y,
                "status": result.get("status"),
                "content_type": result.get("content_type"),
                "is_image": bool(result.get("is_image")),
                "body_preview": result.get("body_preview", ""),
            }
            level["probes"].append(compact)
            status = int(result.get("status") or 0)
            preview = str(result.get("body_preview") or "")
            storage_denied = _storage_access_denied(status, str(result.get("content_type") or ""), preview)
            if status in (401, 429) or (status == 403 and not storage_denied):
                raise MapGenieError(
                    f"Tile preflight was refused at z{z} ({x},{y}) with HTTP {status}. "
                    "This looks like client/rate-limit blocking, so the extractor will not silently downgrade. "
                    + (f"Response: {preview[:180]}" if preview else "")
                )
            if status >= 500:
                raise MapGenieError(f"Tile preflight received HTTP {status} at z{z} ({x},{y}); retry later.")
            if status == 200 and result.get("is_image"):
                level["accessible"] = True
                diagnostics.append(level)
                if progress and z < top:
                    progress(f"z{top} unavailable; using highest downloadable z{z}", 1, 1)
                return z, diagnostics
        diagnostics.append(level)
        if transport_errors == len(points):
            first_error = level["probes"][0].get("error", "unknown error")
            raise MapGenieError(f"Could not verify tile set {tile_set.index} z{z}: all probes failed. First error: {first_error}")
    summary = "; ".join(
        f"z{level['zoom']}=[{','.join(str(p.get('status', 'error')) for p in level['probes'])}]" for level in diagnostics
    )
    raise MapGenieError(f"No downloadable zoom found for tile set {tile_set.index} ({tile_set.name}). {summary}")


def _map_metadata(info: MapInfo) -> dict:
    return {
        "game_slug": info.game_slug,
        "map_slug": info.map_slug,
        "title": info.title,
        "map_id": info.map_id,
        "source_url": info.source_url,
        "tiles_base_url": info.tiles_base_url,
    }


def _tileset_metadata(ts: TileSetInfo) -> dict:
    return {
        "index": ts.index,
        "name": ts.name,
        "pattern": ts.pattern,
        "extension": ts.extension,
        "min_zoom": ts.min_zoom,
        "max_zoom": ts.max_zoom,
        "bounds": {str(z): asdict(b) for z, b in ts.bounds.items()},
    }


def download_highest_zoom(
    map_info: MapInfo,
    tile_set: TileSetInfo,
    output_dir: Path,
    concurrency: int = 4,
    retries: int = 5,
    request_delay: float = 0.12,
    progress: ProgressCallback | None = None,
    verify: bool | str = DEFAULT_TLS_VERIFY,
    transport: str = "auto",
    max_zoom: int | None = None,
) -> dict:
    transport = _resolve_tile_transport(map_info.source_url, transport)
    z, preflight = find_highest_downloadable_zoom(map_info, tile_set, verify, transport, progress=progress, max_zoom=max_zoom)
    bounds = tile_set.bounds_for(z)
    ext = tile_set.extension.lower().lstrip(".") or "png"
    tile_dir = output_dir / "tiles" / f"tileset_{tile_set.index}" / str(z)
    tile_dir.mkdir(parents=True, exist_ok=True)
    total = bounds.count
    stats = {"downloaded": 0, "cached": 0, "missing": 0, "failed": 0, "rate_limited": 0, "total": total}
    pacer = RequestPacer(request_delay)
    completed = 0
    state_lock = threading.Lock()
    attempts = max(1, retries)

    def one(job: tuple[int, int]) -> tuple[str, int, int, str]:
        nonlocal completed
        x, y = job
        path = tile_dir / str(x) / f"{y}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 0:
            return "cached", x, y, ""
        url = make_tile_url(map_info, tile_set, z, x, y)
        session = _thread_session(verify, map_info.source_url, transport)
        last_error = ""
        for attempt in range(attempts):
            try:
                pacer.wait()
                response = session.get(url, timeout=30)
                if response.status_code == 404 or _is_storage_access_denied_response(response):
                    return "missing", x, y, url
                if response.status_code in (403, 429):
                    with state_lock:
                        stats["rate_limited"] += 1
                        current = completed
                    cooldown = pacer.penalize(response.status_code, response.headers.get("Retry-After"))
                    last_error = f"HTTP {response.status_code}; cooldown {cooldown:.0f}s (attempt {attempt + 1}/{attempts})"
                    if progress:
                        progress(f"HTTP {response.status_code} from tile CDN; cooling down {cooldown:.0f}s", current, total)
                    continue
                response.raise_for_status()
                data = response.content
                if not _looks_like_image(data):
                    last_error = f"Non-image response ({response.headers.get('content-type')})"
                    time.sleep(0.5 * (attempt + 1))
                    continue
                tmp = path.with_suffix(path.suffix + ".part")
                tmp.write_bytes(data)
                tmp.replace(path)
                return "downloaded", x, y, ""
            except _TILE_REQUEST_EXCEPTIONS as exc:
                last_error = str(exc)
                time.sleep(min(5.0, 0.75 * (attempt + 1)))
            except OSError as exc:
                last_error = str(exc)
                time.sleep(min(3.0, 0.5 * (attempt + 1)))
        return "failed", x, y, last_error

    def jobs():
        for x in range(bounds.x_min, bounds.x_max + 1):
            for y in range(bounds.y_min, bounds.y_max + 1):
                yield x, y

    workers = max(1, concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        iterator = iter(jobs())
        pending = set()
        for _ in range(min(total, workers * 4)):
            try:
                pending.add(pool.submit(one, next(iterator)))
            except StopIteration:
                break
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                status, x, y, detail = future.result()
                with state_lock:
                    completed += 1
                    stats[status] += 1
                    current = completed
                if progress:
                    suffix = f" ({x},{y})" + (f" failed: {detail}" if status == "failed" and detail else "")
                    progress(status + suffix, current, total)
                try:
                    pending.add(pool.submit(one, next(iterator)))
                except StopIteration:
                    pass

    tile_meta = _tileset_metadata(tile_set)
    tile_meta.update(
        {
            "advertised_max_zoom": tile_set.max_zoom,
            "selected_zoom": z,
            "highest_downloadable_zoom": z,
            "zoom_fallback_used": z != preflight[0]["zoom"],
            "zoom_preflight": preflight,
            "bounds": asdict(bounds),
        }
    )
    manifest = {
        "map": _map_metadata(map_info),
        "tile_set": tile_meta,
        "request_policy": {
            "concurrency": workers,
            "retries": attempts,
            "initial_request_delay_seconds": max(0.0, float(request_delay)),
            "final_request_delay_seconds": pacer.request_delay,
            "referer": _browser_referer(map_info.source_url),
            "transport": transport,
            "chrome_impersonation": "chrome" if transport == "chrome" else None,
        },
        "download": stats,
    }
    (output_dir / f"tileset_{tile_set.index}_z{z}_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _png_chunk(file_obj, kind: bytes, payload: bytes) -> None:
    file_obj.write(struct.pack(">I", len(payload)))
    file_obj.write(kind)
    file_obj.write(payload)
    file_obj.write(struct.pack(">I", zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF))


def _find_first_tile(tile_dir: Path, bounds: TileBounds, ext: str) -> Path | None:
    for x in range(bounds.x_min, bounds.x_max + 1):
        for y in range(bounds.y_min, bounds.y_max + 1):
            path = tile_dir / str(x) / f"{y}.{ext}"
            if path.exists() and path.stat().st_size > 0:
                return path
    return None


def stitch_to_png(
    map_info: MapInfo,
    tile_set: TileSetInfo,
    output_dir: Path,
    output_file: Path | None = None,
    progress: ProgressCallback | None = None,
    zoom: int | None = None,
) -> Path:
    z = tile_set.max_zoom if zoom is None else int(zoom)
    bounds = tile_set.bounds_for(z)
    ext = tile_set.extension.lower().lstrip(".") or "png"
    tile_dir = output_dir / "tiles" / f"tileset_{tile_set.index}" / str(z)
    first = _find_first_tile(tile_dir, bounds, ext)
    if not first:
        raise MapGenieError(f"No downloaded tiles found under {tile_dir}")
    with Image.open(first) as sample:
        tile_w, tile_h = sample.size
    width, height = bounds.columns * tile_w, bounds.rows * tile_h
    if output_file is None:
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", map_info.map_slug).strip("_")
        output_file = output_dir / f"{name}__tileset-{tile_set.index}__z{z}.png"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Stream one tile-row at a time; never allocate the full mosaic.
    with output_file.open("wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        _png_chunk(f, b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        compressor, buffer = zlib.compressobj(level=6), bytearray()
        stride, blank = tile_w * 4, b"\x00" * (tile_w * 4)
        for row_no, y in enumerate(range(bounds.y_min, bounds.y_max + 1), 1):
            tiles: list[bytes | None] = []
            for x in range(bounds.x_min, bounds.x_max + 1):
                path = tile_dir / str(x) / f"{y}.{ext}"
                try:
                    with Image.open(path) as img:
                        rgba = img.convert("RGBA")
                        if rgba.size != (tile_w, tile_h):
                            rgba = rgba.resize((tile_w, tile_h))
                        tiles.append(rgba.tobytes())
                except Exception:
                    tiles.append(None)
            for pixel_row in range(tile_h):
                start = pixel_row * stride
                scanline = b"\x00" + b"".join(blank if data is None else data[start : start + stride] for data in tiles)
                buffer.extend(compressor.compress(scanline))
                while len(buffer) >= 1 << 20:
                    _png_chunk(f, b"IDAT", bytes(buffer[: 1 << 20]))
                    del buffer[: 1 << 20]
            if progress:
                progress("stitching", row_no, bounds.rows)
        buffer.extend(compressor.flush())
        while buffer:
            _png_chunk(f, b"IDAT", bytes(buffer[: 1 << 20]))
            del buffer[: 1 << 20]
        _png_chunk(f, b"IEND", b"")
    return output_file


def describe_map(map_info: MapInfo) -> str:
    lines = [
        f"{map_info.title} ({map_info.game_slug}/{map_info.map_slug})",
        f"  source: {map_info.source_url}",
        f"  tiles base: {map_info.tiles_base_url}",
        f"  tile sets: {len(map_info.tile_sets)}",
    ]
    for ts in map_info.tile_sets:
        try:
            b = ts.bounds_for(ts.max_zoom)
            bounds = f"x={b.x_min}..{b.x_max}, y={b.y_min}..{b.y_max}, tiles={b.count} ({b.columns}x{b.rows})"
        except Exception as exc:
            bounds = f"bounds unavailable: {exc}"
        lines += [
            f"  [{ts.index}] {ts.name}: advertised z{ts.min_zoom}..z{ts.max_zoom}, .{ts.extension}, {bounds}",
            f"      pattern: {ts.pattern}",
        ]
    return "\n".join(lines)


def describe_map_with_availability(map_info: MapInfo, verify: bool | str = DEFAULT_TLS_VERIFY, transport: str = "auto", max_zoom: int | None = None) -> str:
    lines = [describe_map(map_info), "  live tile availability:"]
    for ts in map_info.tile_sets:
        target = ts.max_zoom if max_zoom is None else min(ts.max_zoom, max_zoom)
        z, diagnostics = find_highest_downloadable_zoom(map_info, ts, verify, transport, max_zoom=max_zoom)
        rejected = ", ".join(f"z{d['zoom']}" for d in diagnostics if not d.get("accessible"))
        detail = f"target z{target}" if z == target else f"target z{target} rejected ({rejected})"
        lines.append(f"    [{ts.index}] {ts.name}: highest downloadable z{z}; {detail}")
    return "\n".join(lines)


def process_map(
    client: MapGenieClient,
    url: str,
    output_root: Path,
    concurrency: int,
    stitch: bool,
    selected_tileset: int | None,
    progress: ProgressCallback | None = None,
    retries: int = 5,
    request_delay: float = 0.12,
    tile_transport: str = "auto",
    max_zoom: int | None = None,
) -> list[Path]:
    info = client.inspect_map(url)
    out = output_root / info.game_slug / info.map_slug
    out.mkdir(parents=True, exist_ok=True)
    meta = _map_metadata(info)
    meta["tile_sets"] = [_tileset_metadata(ts) for ts in info.tile_sets]
    (out / "map_info.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    sets = info.tile_sets if selected_tileset is None else [ts for ts in info.tile_sets if ts.index == selected_tileset]
    if not sets:
        raise MapGenieError(f"Tile set index {selected_tileset} was not found")
    stitched: list[Path] = []
    for ts in sets:
        if progress:
            progress(f"Downloading {info.map_slug} / tile set {ts.index}", 0, 1)
        manifest = download_highest_zoom(
            info, ts, out, concurrency, retries, request_delay, progress, client.verify, tile_transport, max_zoom
        )
        z = int(manifest["tile_set"]["selected_zoom"])
        if stitch:
            if progress:
                progress(f"Stitching {info.map_slug} / tile set {ts.index}", 0, 1)
            stitched.append(stitch_to_png(info, ts, out, progress=progress, zoom=z))
    return stitched


def _cli_progress(message: str, current: int, total: int) -> None:
    if not total:
        print(message)
        return
    print(f"\r[{current * 100 / total:6.2f}%] {message[:110]:110}", end="", flush=True)
    if current >= total:
        print()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Download and stitch MapGenie-backed maps at each tile set's highest actually downloadable zoom.")
    p.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    p.add_argument("url", help="Map page URL, e.g. https://mapgenie.io/.../maps/... or https://rdr2map.com/")
    p.add_argument("--output", default="output", help="Output directory (default: output)")
    p.add_argument("--open-output", action="store_true", help="Open output directory after a successful extraction")
    p.add_argument("--all-maps", action="store_true", help="Also process sibling canonical mapgenie.io map pages")
    p.add_argument("--inspect", action="store_true", help="Inspect config and live maximum zoom only")
    p.add_argument("--tileset", type=int, help="Only process one tile-set index")
    p.add_argument("--zoom", type=int, help="Maximum zoom to use; falls back lower if unavailable")
    p.add_argument("--no-stitch", action="store_true", help="Download tiles without stitching")
    p.add_argument("--concurrency", type=int, default=4, help="Concurrent tile workers (default: 4)")
    p.add_argument("--request-delay", type=float, default=0.12, help="Global delay between requests (default: 0.12s)")
    p.add_argument("--retries", type=int, default=5, help="Attempts per tile (default: 5)")
    p.add_argument("--transport", choices=("auto", "chrome", "requests"), default="auto", help="Tile HTTP transport")
    p.add_argument("--probe-tile", metavar="TILESET,Z,X,Y", help="Fetch one tile and print diagnostics")
    tls = p.add_mutually_exclusive_group()
    tls.add_argument("--ca-bundle", help="PEM CA bundle for HTTPS")
    tls.add_argument("--insecure", action="store_true", help="Disable HTTPS verification (unsafe)")
    p.add_argument("--tls-status", action="store_true", help="Print TLS trust backend")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    verify: bool | str | None = False if args.insecure else None
    if args.ca_bundle:
        path = Path(args.ca_bundle).expanduser().resolve()
        if not path.is_file():
            print(f"ERROR: CA bundle not found: {path}", file=sys.stderr)
            return 2
        verify = str(path)
    client = MapGenieClient(verify=verify)
    if args.tls_status:
        effective = "DISABLED (--insecure)" if client.verify is False else client.verify
        print(f"{client.tls_status()} | effective verify={effective}")
    try:
        if args.probe_tile:
            try:
                parts = [int(v.strip()) for v in args.probe_tile.split(",")]
            except ValueError as exc:
                raise MapGenieError("--probe-tile must be TILESET,Z,X,Y using integers") from exc
            if len(parts) != 4:
                raise MapGenieError("--probe-tile must be TILESET,Z,X,Y, e.g. 0,7,95,56")
            index, z, x, y = parts
            info = client.inspect_map(args.url)
            ts = next((ts for ts in info.tile_sets if ts.index == index), None)
            if ts is None:
                raise MapGenieError(f"Tile set index {index} was not found")
            result = probe_tile(info, ts, z, x, y, client.verify, args.transport)
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == 200 and result["is_image"] else 2

        urls = client.map_urls_for_game(args.url) if args.all_maps else [args.url]
        if args.inspect:
            for url in urls:
                print(describe_map_with_availability(client.inspect_map(url), client.verify, args.transport, args.zoom), "\n")
            return 0

        started = time.perf_counter()
        output = Path(args.output).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        stitched: list[Path] = []
        for url in urls:
            print(f"\nProcessing {url}")
            stitched += process_map(
                client,
                url,
                output,
                max(1, args.concurrency),
                not args.no_stitch,
                args.tileset,
                _cli_progress,
                max(1, args.retries),
                max(0.0, args.request_delay),
                args.transport,
                args.zoom,
            )
        if stitched:
            print("\nStitched files:")
            print("\n".join(f"  {path}" for path in stitched))
        print(f"\nExtraction complete in {format_elapsed(time.perf_counter() - started)}")
        if args.open_output:
            try:
                open_folder(output)
            except OSError as exc:
                print(f"WARNING: Could not open output folder: {exc}", file=sys.stderr)
        return 0
    except (MapGenieError, requests.RequestException, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
