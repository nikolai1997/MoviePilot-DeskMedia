from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import re
import secrets
import threading
import time
import urllib.parse
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import Request
from PIL import Image, ImageOps
from starlette.responses import JSONResponse, Response

from app.chain.media import MediaChain
from app.chain.recommend import RecommendChain
from app.chain.subscribe import SubscribeChain
from app.db.subscribe_oper import SubscribeOper
from app.helper.image import ImageHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaType


class DeskMedia(_PluginBase):
    plugin_name = "桌面摆件影视"
    plugin_desc = "NAS 直连，为 ESP32 等局域网桌面屏提供分类影视、海报、语音搜索和确认订阅。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/subscribe.png"
    plugin_version = "0.3.0"
    plugin_author = "Nikolai"
    plugin_config_prefix = "DeskMedia_"
    plugin_order = 30
    auth_level = 1

    _enable = False
    _device_token = ""
    _feed_size = 12
    _poster_width = 150
    _poster_height = 225
    _poster_max_bytes = 5 * 1024 * 1024
    _poster_max_pixels = 20 * 1024 * 1024
    _item_ttl_seconds = 30 * 60
    _id_pattern = re.compile(r"^[0-9a-f]{20}$")
    _nonce_pattern = re.compile(r"^[0-9a-f]{32}$")
    _poster_hosts = ("image.tmdb.org", "doubanio.com", "douban.com", "m.media-amazon.com")

    def __init__(self):
        super().__init__()
        self._recommend = RecommendChain()
        self._media = MediaChain()
        self._subscribe = SubscribeChain()
        self._subscribe_oper = SubscribeOper()
        self._image_helper = ImageHelper()
        self._items: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._poster_urls: "OrderedDict[str, str]" = OrderedDict()
        self._poster_cache: "OrderedDict[str, bytes]" = OrderedDict()
        self._recent_nonces: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.RLock()
        self._subscribe_lock = threading.Lock()
        self._search_lock = threading.Lock()
        self._poster_slots = threading.BoundedSemaphore(2)

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enable = bool(config.get("enable", True))
        self._device_token = str(config.get("device_token") or "").strip()
        try:
            self._feed_size = max(4, min(12, int(config.get("feed_size") or 12)))
        except (TypeError, ValueError):
            self._feed_size = 12
        if len(self._device_token) < 24:
            self._device_token = secrets.token_urlsafe(24)
            self.update_config({"enable": self._enable, "device_token": self._device_token,
                                "feed_size": self._feed_size})
            logger.info("桌面摆件影视：已生成新的独立设备令牌")
        if not self._enable:
            self.stop_service()

    def get_state(self) -> bool:
        return self._enable and len(self._device_token) >= 24

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/feed", "endpoint": self.feed, "methods": ["GET"],
             "allow_anonymous": True, "summary": "桌面摆件影视列表"},
            {"path": "/search", "endpoint": self.search, "methods": ["POST"],
             "allow_anonymous": True, "summary": "桌面摆件语音搜索"},
            {"path": "/poster/{poster_id}", "endpoint": self.poster, "methods": ["GET"],
             "allow_anonymous": True, "summary": "桌面摆件海报缩略图"},
            {"path": "/subscribe", "endpoint": self.subscribe, "methods": ["POST"],
             "allow_anonymous": True, "summary": "桌面摆件新增订阅"},
            {"path": "/health", "endpoint": self.health, "methods": ["GET"],
             "allow_anonymous": True, "summary": "桌面摆件接口状态"},
        ]

    @staticmethod
    def _private_headers() -> Dict[str, str]:
        return {"Cache-Control": "private, no-store", "Vary": "X-Desk-Nonce, X-Desk-Signature"}

    def _json(self, payload: Dict[str, Any], status_code: int = 200) -> JSONResponse:
        return JSONResponse(payload, status_code=status_code, headers=self._private_headers())

    @staticmethod
    def _signed_path(request: Request) -> str:
        path = str(request.url.path)
        marker = "/DeskMedia"
        if marker in path:
            path = path.split(marker, 1)[1] or "/"
        query = str(request.url.query)
        return f"{path}?{query}" if query else path

    def _authorized(self, request: Request, body: bytes = b"") -> bool:
        nonce = request.headers.get("x-desk-nonce", "").lower()
        supplied = request.headers.get("x-desk-signature", "").lower()
        if not self._nonce_pattern.fullmatch(nonce) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
            return False
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = f"{request.method.upper()}\n{self._signed_path(request)}\n{body_hash}\n{nonce}"
        expected = hmac.new(self._device_token.encode("utf-8"), canonical.encode("utf-8"),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            return False
        now = time.monotonic()
        with self._lock:
            while self._recent_nonces and (now - next(iter(self._recent_nonces.values())) > 600 or
                                           len(self._recent_nonces) >= 256):
                self._recent_nonces.popitem(last=False)
            if nonce in self._recent_nonces:
                return False
            self._recent_nonces[nonce] = now
        return True

    def _reject(self, status_code: int, detail: str) -> JSONResponse:
        return self._json({"ok": False, "error": detail}, status_code=status_code)

    async def _read_json(self, request: Request, max_bytes: int = 512) -> Tuple[Optional[dict], bytes, Optional[JSONResponse]]:
        data = bytearray()
        try:
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > max_bytes:
                    return None, b"", self._reject(413, "request too large")
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return None, b"", self._reject(400, "invalid json")
        if not isinstance(payload, dict):
            return None, b"", self._reject(400, "invalid json")
        return payload, bytes(data), None

    @staticmethod
    def _media_identity(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        source = data.get("source") or data.get("media_source")
        media_id = data.get("media_id")
        if not source:
            if data.get("tmdb_id") or data.get("tmdbid"):
                source = "themoviedb"
                media_id = data.get("tmdb_id") or data.get("tmdbid")
            elif data.get("douban_id") or data.get("doubanid"):
                source = "douban"
                media_id = data.get("douban_id") or data.get("doubanid")
        if hasattr(source, "value"):
            source = source.value
        return (str(source) if source else None, str(media_id) if media_id is not None else None)

    @staticmethod
    def _type_code(value: Any) -> str:
        raw = value.value if hasattr(value, "value") else str(value or "")
        return "tv" if raw.lower() in ("tv", "电视剧") else "movie"

    @staticmethod
    def _poster_id(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return parsed if minimum <= parsed <= maximum else default

    @staticmethod
    def _bounded_rating(value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return round(parsed, 1) if math.isfinite(parsed) and 0 <= parsed <= 10 else 0.0

    def _poster_url_allowed(self, url: str) -> bool:
        try:
            parsed = urllib.parse.urlsplit(url)
            host = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in (None, 443):
                return False
            if not any(host == allowed or host.endswith(f".{allowed}") for allowed in self._poster_hosts):
                return False
            return True
        except ValueError:
            return False

    def _download_poster(self, url: str) -> bytes:
        if not self._poster_url_allowed(url):
            raise ValueError("poster origin rejected")
        content = self._image_helper.fetch_image(url=url, use_cache=True)
        if not content or len(content) > self._poster_max_bytes:
            raise ValueError("poster too large")
        return content

    def _subscription_keys(self, subscriptions: Optional[Iterable[Any]] = None) -> set:
        keys = set()
        for sub in subscriptions if subscriptions is not None else (self._subscribe_oper.list() or []):
            source = getattr(sub, "media_source", None)
            if hasattr(source, "value"):
                source = source.value
            media_id = getattr(sub, "media_id", None)
            if source and media_id is not None:
                keys.add(f"{source}:{media_id}")
            if getattr(sub, "tmdbid", None) is not None:
                keys.add(f"themoviedb:{sub.tmdbid}")
            if getattr(sub, "doubanid", None):
                keys.add(f"douban:{sub.doubanid}")
        return keys

    def _compact_item(self, data: Dict[str, Any], subscribed: bool = False) -> Optional[Dict[str, Any]]:
        source, media_id = self._media_identity(data)
        title = str(data.get("title") or data.get("name") or "").strip()
        if not source or not media_id or not title or len(title.encode("utf-8")) > 192:
            return None
        media_type = self._type_code(data.get("type"))
        season = self._bounded_int(data.get("season"), 1 if media_type == "tv" else 0, 0, 255)
        item_key = hashlib.sha256(f"{source}:{media_id}:{season}".encode("utf-8")).hexdigest()[:20]
        poster_url = str(data.get("poster_path") or data.get("poster") or "").strip()
        if poster_url and not self._poster_url_allowed(poster_url):
            poster_url = ""
        poster_id = self._poster_id(poster_url) if poster_url else ""
        item = {
            "id": item_key, "title": title[:48], "year": str(data.get("year") or "")[:4],
            "type": media_type, "season": season,
            "rating": self._bounded_rating(data.get("vote_average") or data.get("vote")),
            "subscribed": bool(subscribed), "state": str(data.get("state") or "")[:16],
            "poster_id": poster_id, "source": source, "media_id": media_id,
            "tmdb_id": data.get("tmdb_id") or data.get("tmdbid"),
            "douban_id": data.get("douban_id") or data.get("doubanid"), "issued_at": time.monotonic(),
        }
        with self._lock:
            self._items[item_key] = item
            self._items.move_to_end(item_key)
            if poster_id:
                self._poster_urls[poster_id] = poster_url
                self._poster_urls.move_to_end(poster_id)
            recent_limit = max(16, self._feed_size * 4)
            while len(self._items) > recent_limit:
                self._items.popitem(last=False)
            while len(self._poster_urls) > recent_limit:
                self._poster_urls.popitem(last=False)
        return {key: item[key] for key in (
            "id", "title", "year", "type", "season", "rating", "subscribed", "state", "poster_id")}

    @staticmethod
    def _as_dict(media: Any) -> Dict[str, Any]:
        if isinstance(media, dict):
            return dict(media)
        for method_name in ("model_dump", "dict", "to_dict"):
            method = getattr(media, method_name, None)
            if callable(method):
                value = method()
                if isinstance(value, dict):
                    return value
        names = ("source", "media_source", "media_id", "tmdb_id", "tmdbid", "douban_id", "doubanid",
                 "title", "name", "year", "type", "season", "vote_average", "vote", "poster_path", "poster")
        return {name: getattr(media, name) for name in names if hasattr(media, name)}

    def _popular_items(self, media_type: Optional[str] = None) -> List[Dict[str, Any]]:
        if media_type == "movie":
            return (self._recommend.tmdb_movies(page=1) or [])[:self._feed_size]
        if media_type == "tv":
            return (self._recommend.tmdb_tvs(page=1) or [])[:self._feed_size]
        movie_count = (self._feed_size + 1) // 2
        tv_count = self._feed_size // 2
        movies = self._recommend.tmdb_movies(page=1) or []
        tvs = self._recommend.tmdb_tvs(page=1) or []
        mixed: List[Dict[str, Any]] = []
        for index in range(max(movie_count, tv_count)):
            if index < movie_count and index < len(movies):
                mixed.append(movies[index])
            if index < tv_count and index < len(tvs):
                mixed.append(tvs[index])
        return mixed[:self._feed_size]

    def _subscribed_items(self, subscriptions: Iterable[Any]) -> List[Dict[str, Any]]:
        ordered = sorted(subscriptions, key=lambda sub: str(getattr(sub, "date", "")), reverse=True)
        result = []
        for sub in ordered[:self._feed_size]:
            result.append({
                "title": getattr(sub, "name", ""), "year": getattr(sub, "year", ""),
                "type": getattr(sub, "type", ""), "season": getattr(sub, "season", None),
                "vote": getattr(sub, "vote", 0), "state": getattr(sub, "state", ""),
                "poster": getattr(sub, "poster", ""), "media_source": getattr(sub, "media_source", None),
                "media_id": getattr(sub, "media_id", None), "tmdbid": getattr(sub, "tmdbid", None),
                "doubanid": getattr(sub, "doubanid", None),
            })
        return result

    def health(self, request: Request):
        if not self.get_state():
            return self._reject(503, "plugin disabled")
        if not self._authorized(request):
            return self._reject(401, "invalid device token")
        return self._json({"ok": True, "version": 2, "plugin": self.plugin_version})

    def feed(self, request: Request, view: str = "popular", limit: int = 12):
        if not self.get_state():
            return self._reject(503, "plugin disabled")
        if not self._authorized(request):
            return self._reject(401, "invalid device token")
        if view not in ("popular", "movies", "tv", "subscribed"):
            return self._reject(400, "invalid view")
        limit = max(1, min(self._feed_size, self._bounded_int(limit, self._feed_size, 1, self._feed_size)))
        try:
            subscriptions = self._subscribe_oper.list() or []
            raw_items = self._subscribed_items(subscriptions) if view == "subscribed" else self._popular_items(
                "movie" if view == "movies" else "tv" if view == "tv" else None)
            subscribed_keys = self._subscription_keys(subscriptions)
            items = []
            for raw in raw_items:
                try:
                    raw_dict = self._as_dict(raw)
                    source, media_id = self._media_identity(raw_dict)
                    compact = self._compact_item(raw_dict, subscribed=view == "subscribed" or
                                                 f"{source}:{media_id}" in subscribed_keys)
                except Exception as item_error:
                    logger.warning(f"桌面摆件影视：跳过无效条目：{item_error}")
                    continue
                if compact:
                    items.append(compact)
                if len(items) >= limit:
                    break
            return self._json({"ok": True, "version": 2, "view": view,
                               "generated_at": int(time.time()), "items": items})
        except Exception as feed_error:
            logger.error(f"桌面摆件影视列表生成失败：{feed_error}")
            return self._reject(502, "movie data unavailable")

    async def search(self, request: Request):
        if not self.get_state():
            return self._reject(503, "plugin disabled")
        payload, raw_body, error = await self._read_json(request)
        if error:
            return error
        if not self._authorized(request, raw_body):
            return self._reject(401, "invalid request signature")
        query = str(payload.get("query") or "").strip()
        media_type = str(payload.get("type") or "all").lower()
        limit = self._bounded_int(payload.get("limit"), 3, 1, 3)
        if not query or len(query.encode("utf-8")) > 128:
            return self._reject(422, "invalid query")
        if media_type not in ("all", "movie", "tv"):
            return self._reject(422, "invalid type")
        if not self._search_lock.acquire(blocking=False):
            return self._reject(429, "search busy")
        try:
            _, results = self._media.search(title=query)
            subscriptions = self._subscribe_oper.list() or []
            subscribed_keys = self._subscription_keys(subscriptions)
            items = []
            for raw in results or []:
                try:
                    data = self._as_dict(raw)
                    if media_type != "all" and self._type_code(data.get("type")) != media_type:
                        continue
                    source, media_id = self._media_identity(data)
                    compact = self._compact_item(data, subscribed=f"{source}:{media_id}" in subscribed_keys)
                except Exception as item_error:
                    logger.warning(f"桌面摆件影视：跳过无效搜索结果：{item_error}")
                    continue
                if compact:
                    items.append(compact)
                if len(items) >= limit:
                    break
            return self._json({"ok": True, "version": 2, "query": query, "items": items})
        except Exception as search_error:
            logger.error(f"桌面摆件影视搜索失败：{search_error}")
            return self._reject(502, "search unavailable")
        finally:
            self._search_lock.release()

    def poster(self, poster_id: str, request: Request):
        if not self.get_state():
            return self._reject(503, "plugin disabled")
        if not self._authorized(request):
            return self._reject(401, "invalid device token")
        if not self._id_pattern.fullmatch(poster_id or ""):
            return self._reject(404, "poster not in current feed")
        with self._lock:
            cached = self._poster_cache.get(poster_id)
            url = self._poster_urls.get(poster_id)
            if cached:
                self._poster_cache.move_to_end(poster_id)
        headers = {"Cache-Control": "private, max-age=21600", "Vary": "X-Desk-Nonce, X-Desk-Signature",
                   "X-Image-Format": "RGB565LE", "X-Image-Size": f"{self._poster_width}x{self._poster_height}"}
        if cached:
            return Response(cached, media_type="application/octet-stream", headers=headers)
        if not url:
            return self._reject(404, "poster not in current feed")
        if not self._poster_slots.acquire(blocking=False):
            return self._reject(429, "poster busy")
        try:
            with self._lock:
                cached = self._poster_cache.get(poster_id)
            if cached:
                return Response(cached, media_type="application/octet-stream", headers=headers)
            original = self._download_poster(url)
            with Image.open(io.BytesIO(original)) as image:
                if getattr(image, "n_frames", 1) != 1 or image.width * image.height > self._poster_max_pixels:
                    raise ValueError("poster dimensions rejected")
                fitted = ImageOps.fit(image.convert("RGB"), (self._poster_width, self._poster_height),
                                      method=Image.Resampling.LANCZOS)
                rgb = fitted.tobytes()
                content = bytearray(self._poster_width * self._poster_height * 2)
                output_index = 0
                for index in range(0, len(rgb), 3):
                    red, green, blue = rgb[index], rgb[index + 1], rgb[index + 2]
                    value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
                    content[output_index] = value & 0xFF
                    content[output_index + 1] = value >> 8
                    output_index += 2
                content = bytes(content)
            with self._lock:
                self._poster_cache[poster_id] = content
                self._poster_cache.move_to_end(poster_id)
                while len(self._poster_cache) > 8:
                    self._poster_cache.popitem(last=False)
            return Response(content, media_type="application/octet-stream", headers=headers)
        except Exception as poster_error:
            logger.warning(f"桌面摆件海报处理失败 {poster_id}：{poster_error}")
            return self._reject(502, "poster unavailable")
        finally:
            self._poster_slots.release()

    async def subscribe(self, request: Request):
        if not self.get_state():
            return self._reject(503, "plugin disabled")
        payload, raw_body, error = await self._read_json(request)
        if error:
            return error
        if not self._authorized(request, raw_body):
            return self._reject(401, "invalid request signature")
        item_id = str(payload.get("id") or "")
        if not self._id_pattern.fullmatch(item_id):
            return self._reject(422, "invalid item id")
        with self._lock:
            item = dict(self._items.get(item_id) or {})
        if not item or time.monotonic() - float(item.get("issued_at") or 0) > self._item_ttl_seconds:
            return self._reject(404, "refresh feed before subscribing")
        if item.get("subscribed"):
            return self._json({"ok": True, "status": "exists", "message": "已订阅"})
        try:
            media_type = MediaType.TV if item.get("type") == "tv" else MediaType.MOVIE
            kwargs = {"title": item["title"], "year": item.get("year") or "", "mtype": media_type,
                      "season": item.get("season") or None, "exist_ok": True, "username": "桌面摆件",
                      "media_source": item.get("source"), "media_id": item.get("media_id")}
            if item.get("tmdb_id"):
                kwargs["tmdbid"] = int(item["tmdb_id"])
            if item.get("douban_id"):
                kwargs["doubanid"] = str(item["douban_id"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return self._reject(422, "invalid media metadata")
        with self._subscribe_lock:
            with self._lock:
                if self._items.get(item_id, {}).get("subscribed"):
                    return self._json({"ok": True, "status": "exists", "message": "已订阅"})
            try:
                sid, message = self._subscribe.add(**kwargs)
            except Exception as subscribe_error:
                logger.error(f"桌面摆件订阅 {item.get('title')} 失败：{subscribe_error}")
                return self._reject(502, "subscribe failed")
            if sid or "存在" in str(message):
                with self._lock:
                    if item_id in self._items:
                        self._items[item_id]["subscribed"] = True
                status = "subscribed" if sid else "exists"
                result = {"ok": True, "status": status, "message": message or "订阅成功"}
                if sid:
                    result["subscription_id"] = sid
                return self._json(result)
            return self._reject(422, str(message or "subscribe failed"))

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        form = [{"component": "VForm", "content": [{"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                {"component": "VSwitch", "props": {"model": "enable", "label": "启用插件"}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                {"component": "VTextField", "props": {"model": "feed_size", "label": "列表数量",
                                                          "type": "number", "min": 4, "max": 12}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                {"component": "VTextField", "props": {"model": "device_token", "label": "摆件设备令牌",
                                                          "type": "password", "hint": "至少 24 位，与管理令牌分离"}}]},
        ]}, {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
             "text": "仅供可信局域网或 HTTPS 反向代理使用；令牌只保存在 MoviePilot 与摆件本机。"}}]}]
        return form, {"enable": self._enable, "device_token": self._device_token, "feed_size": self._feed_size}

    def get_page(self) -> Optional[List[dict]]:
        return None

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        with self._lock:
            self._items.clear()
            self._poster_urls.clear()
            self._poster_cache.clear()
            self._recent_nonces.clear()
