import asyncio
import importlib.util
import hashlib
import hmac
import io
import json
import sys
import secrets
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


TOKEN = "test-device-token-at-least-24-chars"


class FakePluginBase:
    def __init__(self):
        self.saved_config = None

    def update_config(self, config):
        self.saved_config = config


class FakeRecommendChain:
    def tmdb_movies(self, page=1):
        return []

    def tmdb_tvs(self, page=1):
        return []


class FakeMediaChain:
    def search(self, title):
        return None, []


class FakeSubscribeChain:
    def __init__(self):
        self.calls = []

    def add(self, **kwargs):
        self.calls.append(kwargs)
        return 42, "ok"


class FakeSubscribeOper:
    def list(self):
        return []


class FakeImageHelper:
    def fetch_image(self, url, use_cache=True):
        return b""


class FakeLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeRequest:
    def __init__(self, token=TOKEN, payload=None, raw=None, method="GET", path="/api/v1/plugin/DeskMedia/feed", query=""):
        if raw is not None:
            self.raw = raw
        else:
            self.raw = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        self.method = method
        self.url = SimpleNamespace(path=path, query=query)
        nonce = secrets.token_hex(16)
        signed_path = path.split("/DeskMedia", 1)[1] or "/"
        if query:
            signed_path += f"?{query}"
        body_hash = hashlib.sha256(self.raw if method == "POST" else b"").hexdigest()
        canonical = f"{method}\n{signed_path}\n{body_hash}\n{nonce}"
        signature = hmac.new(token.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        self.headers = {"x-desk-nonce": nonce, "x-desk-signature": signature}

    async def stream(self):
        yield self.raw


def install_module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    if "." not in name:
        module.__path__ = []
    sys.modules[name] = module
    return module


def load_plugin_module():
    install_module("app")
    install_module("app.chain")
    install_module("app.chain.media", MediaChain=FakeMediaChain)
    install_module("app.chain.recommend", RecommendChain=FakeRecommendChain)
    install_module("app.chain.subscribe", SubscribeChain=FakeSubscribeChain)
    install_module("app.db")
    install_module("app.db.subscribe_oper", SubscribeOper=FakeSubscribeOper)
    install_module("app.helper")
    install_module("app.helper.image", ImageHelper=FakeImageHelper)
    install_module("app.log", logger=FakeLogger())
    install_module("app.plugins", _PluginBase=FakePluginBase)
    install_module("app.schemas", MediaType=SimpleNamespace(TV="tv", MOVIE="movie"))
    path = Path(__file__).parents[1] / "plugins.v2" / "deskmedia" / "__init__.py"
    spec = importlib.util.spec_from_file_location("deskmedia_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(response):
    return json.loads(response.body)


class DeskMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin_module()

    def setUp(self):
        self.plugin = self.module.DeskMedia()
        self.plugin.init_plugin({"enable": True, "device_token": TOKEN, "feed_size": 4})
        self.plugin._poster_url_allowed = lambda url: True
        image = Image.new("RGB", (2, 2), (255, 0, 0))
        output = io.BytesIO()
        image.save(output, format="PNG")
        poster = output.getvalue()
        self.plugin._download_poster = lambda url: poster

    @staticmethod
    def movie(media_id, title, poster="https://image.tmdb.org/t/p/w500/movie.jpg"):
        return {"source": "themoviedb", "media_id": str(media_id), "tmdb_id": media_id,
                "title": title, "year": "2026", "type": "电影", "poster_path": poster,
                "vote_average": 8.2}

    @staticmethod
    def tv(media_id, title, poster="https://image.tmdb.org/t/p/w500/tv.jpg"):
        return {"source": "themoviedb", "media_id": str(media_id), "tmdb_id": media_id,
                "title": title, "year": "2026", "type": "电视剧", "poster_path": poster,
                "vote_average": 7.9}

    def test_rejects_invalid_device_token(self):
        response = self.plugin.feed(FakeRequest("wrong"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_rejects_replayed_signature(self):
        request = FakeRequest()
        self.assertEqual(self.plugin.feed(request).status_code, 200)
        self.assertEqual(self.plugin.feed(request).status_code, 401)

    def test_popular_feed_alternates_movies_and_tv_and_marks_subscription(self):
        self.plugin._recommend.tmdb_movies = lambda page=1: [self.movie(101, "Movie A"), self.movie(102, "Movie B")]
        self.plugin._recommend.tmdb_tvs = lambda page=1: [self.tv(201, "TV A"), self.tv(202, "TV B")]
        self.plugin._subscribe_oper.list = lambda: [SimpleNamespace(
            media_source="themoviedb", media_id="201", tmdbid=201, doubanid=None)]
        result = payload(self.plugin.feed(FakeRequest(), limit=4))
        self.assertEqual([item["title"] for item in result["items"]], ["Movie A", "TV A", "Movie B", "TV B"])
        self.assertFalse(result["items"][0]["subscribed"])
        self.assertTrue(result["items"][1]["subscribed"])
        self.assertEqual(result["items"][1]["season"], 1)

    def test_movie_and_tv_filters_are_separate(self):
        self.plugin._recommend.tmdb_movies = lambda page=1: [self.movie(101, "Movie A"), self.movie(102, "Movie B")]
        self.plugin._recommend.tmdb_tvs = lambda page=1: [self.tv(201, "TV A"), self.tv(202, "TV B")]
        movies = payload(self.plugin.feed(FakeRequest(), view="movies", limit=4))
        tv = payload(self.plugin.feed(FakeRequest(), view="tv", limit=4))
        self.assertEqual([item["title"] for item in movies["items"]], ["Movie A", "Movie B"])
        self.assertTrue(all(item["type"] == "movie" for item in movies["items"]))
        self.assertEqual([item["title"] for item in tv["items"]], ["TV A", "TV B"])
        self.assertTrue(all(item["type"] == "tv" for item in tv["items"]))

    def test_malformed_feed_item_is_skipped(self):
        bad = self.movie(100, "Bad")
        bad["season"] = object()
        bad["vote_average"] = float("nan")
        self.plugin._recommend.tmdb_movies = lambda page=1: [bad, self.movie(101, "Good")]
        self.plugin._recommend.tmdb_tvs = lambda page=1: []
        result = payload(self.plugin.feed(FakeRequest(), limit=2))
        self.assertEqual([item["title"] for item in result["items"]], ["Bad", "Good"])
        self.assertEqual(result["items"][0]["rating"], 0.0)

    def test_rejects_unknown_feed_filter(self):
        response = self.plugin.feed(FakeRequest(), view="anime")
        self.assertEqual(response.status_code, 400)

    def test_poster_is_fixed_size_rgb565_little_endian(self):
        self.plugin._recommend.tmdb_movies = lambda page=1: [self.movie(101, "Movie A")]
        self.plugin._recommend.tmdb_tvs = lambda page=1: []
        feed = payload(self.plugin.feed(FakeRequest(), limit=1))
        response = self.plugin.poster(feed["items"][0]["poster_id"], FakeRequest())
        self.assertEqual(len(response.body), 150 * 225 * 2)
        self.assertEqual(response.body[:2], b"\x00\xf8")
        self.assertEqual(response.headers["x-image-format"], "RGB565LE")

    def test_private_poster_origin_is_rejected(self):
        fresh = self.module.DeskMedia()
        fresh.init_plugin({"enable": True, "device_token": TOKEN, "feed_size": 4})
        self.assertFalse(fresh._poster_url_allowed("http://127.0.0.1/private.png"))
        self.assertFalse(fresh._poster_url_allowed("https://example.com/poster.png"))

    def test_search_returns_bounded_candidates_for_confirmation(self):
        self.plugin._media.search = lambda title: (None, [self.movie(101, "Movie A"), self.tv(201, "TV A")])
        response = asyncio.run(self.plugin.search(FakeRequest(
            payload={"query": "A", "type": "movie", "limit": 3}, method="POST",
            path="/api/v1/plugin/DeskMedia/search")))
        result = payload(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["title"] for item in result["items"]], ["Movie A"])
        self.assertRegex(result["items"][0]["id"], r"^[0-9a-f]{20}$")

    def test_body_size_and_item_id_are_bounded(self):
        too_large = asyncio.run(self.plugin.search(FakeRequest(
            raw=b"{" + b"x" * 600 + b"}", method="POST", path="/api/v1/plugin/DeskMedia/search")))
        self.assertEqual(too_large.status_code, 413)
        rejected = asyncio.run(self.plugin.subscribe(FakeRequest(
            payload={"id": "not-in-feed"}, method="POST", path="/api/v1/plugin/DeskMedia/subscribe")))
        self.assertEqual(rejected.status_code, 422)

    def test_subscribe_uses_only_current_item(self):
        self.plugin._recommend.tmdb_movies = lambda page=1: [self.movie(101, "Movie A")]
        self.plugin._recommend.tmdb_tvs = lambda page=1: []
        feed = payload(self.plugin.feed(FakeRequest(), limit=1))
        item_id = feed["items"][0]["id"]
        response = asyncio.run(self.plugin.subscribe(FakeRequest(
            payload={"id": item_id}, method="POST", path="/api/v1/plugin/DeskMedia/subscribe")))
        self.assertTrue(payload(response)["ok"])
        self.assertEqual(self.plugin._subscribe.calls[0]["tmdbid"], 101)
        self.assertEqual(self.plugin._subscribe.calls[0]["username"], "桌面摆件")

    def test_disabled_plugin_rejects_all_routes(self):
        self.plugin._enable = False
        self.assertEqual(self.plugin.health(FakeRequest()).status_code, 503)
        self.assertEqual(self.plugin.poster("a" * 20, FakeRequest()).status_code, 503)


if __name__ == "__main__":
    unittest.main()
