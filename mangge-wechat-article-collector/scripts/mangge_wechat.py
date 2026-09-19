#!/usr/bin/env python3
"""Cost-bounded WeChat article collection through the public Mangge Cloud API."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


API_ROOT = "https://api.we-media.cn"
CATALOG_PATH = "/api/v1/products"
PUBLIC_PREFIX = "/openapi"
CONFIG_FILE_NAME = ".env"
DEFAULT_OUTPUT_DIR = Path.cwd() / "output" / "mangge-wechat"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MICROS_PER_YUAN = 1_000_000
PAGE_SIZE = 20

SEARCH_SLUG = "wechat-native-search-accounts"
HISTORY_SLUG = "wechat-native-account-articles"
CONTENT_SLUG = "wechat-native-article-content"
REQUIRED_SLUGS = (SEARCH_SLUG, HISTORY_SLUG, CONTENT_SLUG)

GHID_PATTERN = re.compile(
    r"^(?:gh_[0-9a-fA-F]{12,32}|wxid_[0-9]{6,32}|[A-Za-z][A-Za-z0-9_-]{5,31})$"
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


class ManggeError(RuntimeError):
    """A user-safe API or local processing failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        payload: dict[str, Any] | None = None,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload or {}
        self.ambiguous = ambiguous


def emit_json(value: dict[str, Any], output_file: str = "") -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    print(text)
    if output_file:
        atomic_write(Path(output_file).expanduser(), text + "\n")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def default_config_file() -> Path:
    return Path(__file__).absolute().parent.parent / CONFIG_FILE_NAME


def config_file_for(args: argparse.Namespace) -> Path:
    configured = getattr(args, "config_file", "")
    return Path(configured).expanduser() if configured else default_config_file()


def load_api_key(args: argparse.Namespace) -> str:
    load_dotenv(config_file_for(args))
    value = os.environ.get("WE_MEDIA_API_KEY", "").strip()
    if not value:
        raise ManggeError("未配置 WE_MEDIA_API_KEY")
    return value


def write_api_key(path: Path, api_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(f'WE_MEDIA_API_KEY="{api_key}"\n', encoding="utf-8")
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    temporary.replace(path)


def redact(value: Any, secrets: Iterable[str] = ()) -> str:
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"ach_(?:live|test)_[A-Za-z0-9_-]+", "[REDACTED]", text)
    return " ".join(text.split())[:400]


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").lower() in {"1", "true", "yes"}


def normalize_name(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def normalize_url(value: Any) -> str:
    return str(value or "").strip().replace("&amp;", "&")


def sanitize_filename(value: str, max_len: int = 90) -> str:
    value = re.sub(r'[\\/:*?"<>|#\n\r\t]', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or "未命名")[:max_len]


def article_key(url: str, title: str = "", publish_ts: int = 0) -> str:
    parsed = urllib.parse.urlsplit(normalize_url(url))
    query = urllib.parse.parse_qs(parsed.query)
    stable = [query.get(name, [""])[0] for name in ("__biz", "mid", "idx", "sn")]
    source = "|".join(stable) if all(stable) else normalize_url(url)
    if not source:
        source = f"{title}|{publish_ts}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def parse_publish_timestamp(item: dict[str, Any]) -> int:
    timestamp = as_int(item.get("publishTimestamp"), 0)
    if timestamp > 0:
        return timestamp
    text = str(item.get("publishTime") or "").strip()
    if not text:
        return 0
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp())


def beijing_time_text(timestamp: int, fallback: str = "") -> str:
    if timestamp <= 0:
        return fallback
    return dt.datetime.fromtimestamp(timestamp, SHANGHAI_TZ).isoformat(timespec="seconds")


def parse_date_window(start_date: str, end_date: str) -> tuple[int, int]:
    try:
        start = dt.date.fromisoformat(start_date)
        end = dt.date.fromisoformat(end_date)
    except ValueError as exc:
        raise ManggeError("日期必须使用 YYYY-MM-DD") from exc
    if end < start:
        raise ManggeError("结束日期不能早于开始日期")
    start_at = dt.datetime.combine(start, dt.time.min, SHANGHAI_TZ)
    end_at = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, SHANGHAI_TZ)
    return int(start_at.timestamp()), int(end_at.timestamp())


def micros_to_yuan_text(micros: int) -> str:
    value = Decimal(micros) / Decimal(MICROS_PER_YUAN)
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def yuan_to_micros(value: str | float | Decimal) -> int:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ManggeError("--max-cost 必须是人民币金额") from exc
    if amount < 0:
        raise ManggeError("--max-cost 不能为负数")
    return int((amount * MICROS_PER_YUAN).to_integral_value(rounding=ROUND_FLOOR))


def find_slug(value: Any, slug: str, seen: set[int] | None = None) -> dict[str, Any] | None:
    if seen is None:
        seen = set()
    if not isinstance(value, (dict, list)) or id(value) in seen:
        return None
    seen.add(id(value))
    if isinstance(value, dict):
        if value.get("slug") == slug:
            return value
        children = value.values()
    else:
        children = value
    for child in children:
        found = find_slug(child, slug, seen)
        if found:
            return found
    return None


def _matching_bps(spec: Any, payload: dict[str, Any]) -> list[int]:
    """Select explicit adjustments that match this request; unknown shapes are ignored."""
    values: list[int] = []
    if isinstance(spec, (int, float)):
        values.append(max(0, int(spec)))
    elif isinstance(spec, dict):
        for key, value in spec.items():
            if key in payload:
                if isinstance(value, dict):
                    selected = value.get(str(payload[key]))
                    if isinstance(selected, (int, float)):
                        values.append(max(0, int(selected)))
                elif isinstance(value, (int, float)):
                    values.append(max(0, int(value)))
            elif key in {str(v) for v in payload.values()} and isinstance(value, (int, float)):
                values.append(max(0, int(value)))
    elif isinstance(spec, list):
        for entry in spec:
            if not isinstance(entry, dict):
                continue
            parameter = str(entry.get("parameter") or entry.get("name") or "")
            expected = entry.get("value")
            bps = entry.get("bps")
            if parameter in payload and str(payload[parameter]) == str(expected) and isinstance(bps, (int, float)):
                values.append(max(0, int(bps)))
    return values


@dataclass(frozen=True)
class Product:
    slug: str
    method: str
    path: str
    price_micros: int
    billing_unit: str
    raw: dict[str, Any]

    def endpoint(self) -> str:
        if self.method != "POST":
            raise ManggeError(f"商品 {self.slug} 的公开方法不是 POST，已停止")
        if not self.path.startswith("/") or ".." in self.path or "://" in self.path:
            raise ManggeError(f"商品 {self.slug} 的公开路径不安全，已停止")
        return f"{API_ROOT}{PUBLIC_PREFIX}/{self.slug}{self.path}"

    def estimate_micros(self, payload: dict[str, Any], quantity: int = 1) -> int:
        bps_values: list[int] = []
        for key in ("priceAdjustmentsBps", "valuePriceAdjustmentsBps"):
            bps_values.extend(_matching_bps(self.raw.get(key), payload))
        # This workflow only sends canonical mp.weixin.qq.com URLs, never short links.
        highest_bps = max(bps_values, default=0)
        return math.floor(self.price_micros * quantity * (10_000 + highest_bps) / 10_000)


class ManggeClient:
    def __init__(self, api_key: str, timeout: int = 30) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.prefer_curl = False

    @staticmethod
    def _curl_quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _curl_request_json(
        self,
        url: str,
        *,
        method: str,
        payload: dict[str, Any] | None,
        idempotency_key: str,
        attempts: int,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        curl = shutil.which("curl") or shutil.which("curl.exe")
        if not curl:
            raise ManggeError("本机 HTTPS 通道不可用，且未找到 curl", code="HTTPS_UNAVAILABLE")
        last_error = ""
        with tempfile.TemporaryDirectory(prefix="mangge-http-") as folder:
            temporary = Path(folder)
            body_path = temporary / "response.json"
            header_path = temporary / "headers.txt"
            request_path = temporary / "request.json"
            if payload is not None:
                request_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            config = [
                "silent",
                "show-error",
                f"max-time = {max(1, self.timeout)}",
                f"request = {self._curl_quote(method)}",
                f"url = {self._curl_quote(url)}",
                f"header = {self._curl_quote('X-API-Key: ' + self.api_key)}",
                f"header = {self._curl_quote('Accept: application/json')}",
                f"dump-header = {self._curl_quote(header_path.as_posix())}",
                f"output = {self._curl_quote(body_path.as_posix())}",
                'write-out = "%{http_code}"',
            ]
            if payload is not None:
                config.extend(
                    [
                        f"header = {self._curl_quote('Content-Type: application/json')}",
                        f"data-binary = {self._curl_quote('@' + request_path.as_posix())}",
                    ]
                )
            if idempotency_key:
                config.append(f"header = {self._curl_quote('Idempotency-Key: ' + idempotency_key)}")
            config_text = "\n".join(config) + "\n"
            for attempt in range(max(1, attempts)):
                result = subprocess.run(
                    [curl, "--config", "-"],
                    input=config_text,
                    text=True,
                    capture_output=True,
                    timeout=max(2, self.timeout + 2),
                    check=False,
                )
                if result.returncode != 0:
                    last_error = redact(result.stderr, (self.api_key,))
                    if attempt + 1 < attempts:
                        time.sleep(0.5)
                        continue
                    raise ManggeError(
                        f"网络结果不明确：{last_error or 'curl 请求失败'}",
                        code="AMBIGUOUS_NETWORK_FAILURE",
                        ambiguous=True,
                    )
                status_text = result.stdout.strip()[-3:]
                status = int(status_text) if status_text.isdigit() else 0
                raw = body_path.read_text(encoding="utf-8", errors="replace") if body_path.exists() else ""
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ManggeError("接口未返回有效 JSON") from exc
                headers: dict[str, str] = {}
                if header_path.exists():
                    for line in header_path.read_text(encoding="utf-8", errors="replace").splitlines():
                        if ":" in line:
                            key, value = line.split(":", 1)
                            headers[key.strip().lower()] = value.strip()
                if status >= 400:
                    code = str(parsed.get("code") or f"HTTP_{status}") if isinstance(parsed, dict) else f"HTTP_{status}"
                    message = "曼格云接口请求失败"
                    if isinstance(parsed, dict):
                        message = str(parsed.get("message") or parsed.get("error") or message)
                    raise ManggeError(redact(message, (self.api_key,)), code=code, payload=parsed)
                if not isinstance(parsed, dict):
                    raise ManggeError("接口返回的 JSON 不是对象")
                return parsed, headers
        raise ManggeError(last_error or "curl 请求失败", ambiguous=True)

    def _request_json(
        self,
        url: str,
        *,
        method: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str = "",
        retry_network_once: bool = False,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if self.prefer_curl:
            return self._curl_request_json(
                url,
                method=method,
                payload=payload,
                idempotency_key=idempotency_key,
                attempts=2 if retry_network_once else 1,
            )
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        attempts = 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                    parsed = json.loads(raw)
                    response_headers = {key.lower(): value for key, value in response.headers.items()}
                    if not isinstance(parsed, dict):
                        raise ManggeError("接口返回的 JSON 不是对象")
                    return parsed, response_headers
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {}
                code = str(parsed.get("code") or f"HTTP_{exc.code}") if isinstance(parsed, dict) else f"HTTP_{exc.code}"
                message = "曼格云接口请求失败"
                if isinstance(parsed, dict):
                    message = str(parsed.get("message") or parsed.get("error") or message)
                raise ManggeError(redact(message, (self.api_key,)), code=code, payload=parsed) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                self.prefer_curl = True
                return self._curl_request_json(
                    url,
                    method=method,
                    payload=payload,
                    idempotency_key=idempotency_key,
                    attempts=1,
                )
            except json.JSONDecodeError as exc:
                raise ManggeError("接口未返回有效 JSON") from exc
        raise ManggeError(redact(last_error, (self.api_key,)), ambiguous=True)

    def product(self, slug: str) -> Product:
        query = urllib.parse.urlencode({"page": 1, "pageSize": 100, "search": slug})
        payload, _ = self._request_json(f"{API_ROOT}{CATALOG_PATH}?{query}", method="GET")
        product = find_slug(payload, slug)
        if not product:
            raise ManggeError(f"实时目录中未找到商品：{slug}")
        method = str(product.get("publicMethod") or "").upper()
        path = str(product.get("publicPath") or "")
        price = as_int(product.get("priceMicros"), -1)
        if price < 0 or not method or not path:
            raise ManggeError(f"商品 {slug} 的实时目录字段不完整")
        return Product(
            slug=slug,
            method=method,
            path=path,
            price_micros=price,
            billing_unit=str(product.get("billingUnit") or ""),
            raw=product,
        )

    def products(self) -> dict[str, Product]:
        return {slug: self.product(slug) for slug in REQUIRED_SLUGS}

    def call(self, product: Product, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        idempotency_key = str(uuid.uuid4())
        response, headers = self._request_json(
            product.endpoint(),
            method="POST",
            payload=payload,
            idempotency_key=idempotency_key,
            retry_network_once=True,
        )
        code = str(response.get("code") or "")
        if code and code != "OK":
            raise ManggeError("曼格云返回非成功结果", code=code, payload=response)
        charge_header = headers.get("x-charge-micros", "").strip()
        if charge_header.isdigit():
            charge_micros = int(charge_header)
        else:
            consumption = Decimal(str(response.get("consumption") or "0"))
            charge_micros = int((consumption * MICROS_PER_YUAN).to_integral_value(rounding=ROUND_FLOOR))
        return response, max(0, charge_micros)


@dataclass
class CostLedger:
    max_micros: int
    spent_micros: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)

    def authorize(self, product: Product, payload: dict[str, Any]) -> int:
        estimate = product.estimate_micros(payload)
        if self.spent_micros + estimate > self.max_micros:
            raise ManggeError("下一次调用将超过已确认的费用上限，已停止", code="BUDGET_EXHAUSTED")
        return estimate

    def record(self, product: Product, actual_micros: int) -> None:
        self.spent_micros += actual_micros
        self.calls.append({"slug": product.slug, "actual_micros": actual_micros})


class ArchiveIndex:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS account_map (
                query_key TEXT PRIMARY KEY,
                query_text TEXT NOT NULL,
                account_name TEXT NOT NULL,
                ghid TEXT NOT NULL,
                alias TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                verification TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS search_cache (
                query_key TEXT NOT NULL,
                position INTEGER NOT NULL,
                account_name TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                alias TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                verification TEXT NOT NULL DEFAULT '',
                latest_update TEXT NOT NULL DEFAULT '',
                searched_at TEXT NOT NULL,
                PRIMARY KEY (query_key, position)
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                ghid TEXT PRIMARY KEY,
                collection_id TEXT NOT NULL DEFAULT '',
                next_page INTEGER,
                has_more INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pages (
                ghid TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                page INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                saved_at TEXT NOT NULL,
                PRIMARY KEY (ghid, collection_id, page)
            );
            CREATE TABLE IF NOT EXISTS articles (
                article_key TEXT PRIMARY KEY,
                ghid TEXT NOT NULL,
                account_name TEXT NOT NULL DEFAULT '',
                article_url TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                digest TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                publish_ts INTEGER NOT NULL DEFAULT 0,
                publish_time TEXT NOT NULL DEFAULT '',
                content_type TEXT NOT NULL DEFAULT '',
                link_status TEXT NOT NULL DEFAULT '',
                body_text TEXT NOT NULL DEFAULT '',
                body_status TEXT NOT NULL DEFAULT 'missing',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_articles_account_time
            ON articles (ghid, publish_ts DESC);
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ArchiveIndex":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @staticmethod
    def now_text() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    def mapping(self, query: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM account_map WHERE query_key = ?", (normalize_name(query),)
        ).fetchone()
        return dict(row) if row else None

    def save_mapping(self, query: str, candidate: dict[str, Any], ghid: str) -> None:
        self.connection.execute(
            """
            INSERT INTO account_map
                (query_key, query_text, account_name, ghid, alias, description, verification, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(query_key) DO UPDATE SET
                query_text=excluded.query_text,
                account_name=excluded.account_name,
                ghid=excluded.ghid,
                alias=excluded.alias,
                description=excluded.description,
                verification=excluded.verification,
                updated_at=excluded.updated_at
            """,
            (
                normalize_name(query),
                query,
                candidate_account_name(candidate) or query,
                ghid,
                str(candidate.get("alias") or ""),
                str(candidate.get("description") or ""),
                str(candidate.get("verification") or ""),
                self.now_text(),
            ),
        )
        self.connection.commit()

    def save_search(self, query: str, items: list[dict[str, Any]]) -> None:
        key = normalize_name(query)
        now = self.now_text()
        with self.connection:
            self.connection.execute("DELETE FROM search_cache WHERE query_key = ?", (key,))
            for position, item in enumerate(items, 1):
                self.connection.execute(
                    """
                    INSERT INTO search_cache
                        (query_key, position, account_name, description, alias, username,
                         verification, latest_update, searched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        position,
                        str(item.get("accountName") or ""),
                        str(item.get("description") or ""),
                        str(item.get("alias") or ""),
                        str(item.get("username") or ""),
                        str(item.get("verification") or ""),
                        str(item.get("latestUpdate") or ""),
                        now,
                    ),
                )

    def search_results(self, query: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM search_cache WHERE query_key = ? ORDER BY position",
            (normalize_name(query),),
        ).fetchall()
        return [dict(row) for row in rows]

    def checkpoint(self, ghid: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM checkpoints WHERE ghid = ?", (ghid,)).fetchone()
        return dict(row) if row else None

    def save_checkpoint(self, ghid: str, collection_id: str, next_page: int | None, has_more: bool) -> None:
        self.connection.execute(
            """
            INSERT INTO checkpoints (ghid, collection_id, next_page, has_more, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ghid) DO UPDATE SET
                collection_id=excluded.collection_id,
                next_page=excluded.next_page,
                has_more=excluded.has_more,
                updated_at=excluded.updated_at
            """,
            (ghid, collection_id, next_page, int(has_more), self.now_text()),
        )
        self.connection.commit()

    def save_error_checkpoint(self, ghid: str, payload: dict[str, Any]) -> None:
        details = payload.get("details") if isinstance(payload, dict) else None
        if not isinstance(details, dict):
            return
        collection_id = str(details.get("collectionId") or "")
        next_page = details.get("nextPage")
        if collection_id and isinstance(next_page, int):
            self.save_checkpoint(ghid, collection_id, next_page, True)

    def save_page(
        self,
        ghid: str,
        account_name: str,
        collection_id: str,
        page: int,
        items: list[dict[str, Any]],
    ) -> str:
        keys: list[str] = []
        now = self.now_text()
        with self.connection:
            for item in items:
                url = normalize_url(item.get("canonicalUrl") or item.get("url") or "")
                title = str(item.get("title") or "").strip()
                publish_ts = parse_publish_timestamp(item)
                key = article_key(url, title, publish_ts)
                keys.append(key)
                self.connection.execute(
                    """
                    INSERT INTO articles
                        (article_key, ghid, account_name, article_url, title, digest, author,
                         publish_ts, publish_time, content_type, link_status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(article_key) DO UPDATE SET
                        ghid=excluded.ghid,
                        account_name=excluded.account_name,
                        article_url=CASE WHEN excluded.article_url <> '' THEN excluded.article_url ELSE articles.article_url END,
                        title=CASE WHEN excluded.title <> '' THEN excluded.title ELSE articles.title END,
                        digest=excluded.digest,
                        author=excluded.author,
                        publish_ts=excluded.publish_ts,
                        publish_time=excluded.publish_time,
                        content_type=excluded.content_type,
                        link_status=excluded.link_status,
                        updated_at=excluded.updated_at
                    """,
                    (
                        key,
                        ghid,
                        account_name,
                        url,
                        title,
                        str(item.get("digest") or ""),
                        str(item.get("author") or ""),
                        publish_ts,
                        beijing_time_text(publish_ts, str(item.get("publishTime") or "")),
                        str(item.get("contentType") or ""),
                        str(item.get("linkStatus") or ""),
                        now,
                    ),
                )
            fingerprint = hashlib.sha256("\n".join(sorted(keys)).encode("utf-8")).hexdigest()
            self.connection.execute(
                """
                INSERT INTO pages (ghid, collection_id, page, fingerprint, saved_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ghid, collection_id, page) DO UPDATE SET
                    fingerprint=excluded.fingerprint,
                    saved_at=excluded.saved_at
                """,
                (ghid, collection_id, page, fingerprint, now),
            )
        return fingerprint

    def save_body(self, key: str, content: str, article: dict[str, Any]) -> None:
        self.connection.execute(
            """
            UPDATE articles SET
                body_text=?,
                body_status='ok',
                title=CASE WHEN ? <> '' THEN ? ELSE title END,
                author=CASE WHEN ? <> '' THEN ? ELSE author END,
                account_name=CASE WHEN ? <> '' THEN ? ELSE account_name END,
                updated_at=?
            WHERE article_key=?
            """,
            (
                content,
                str(article.get("title") or ""),
                str(article.get("title") or ""),
                str(article.get("author") or ""),
                str(article.get("author") or ""),
                str(article.get("accountName") or ""),
                str(article.get("accountName") or ""),
                self.now_text(),
                key,
            ),
        )
        self.connection.commit()

    def coverage(self, ghid: str) -> tuple[int, int]:
        row = self.connection.execute(
            "SELECT MIN(publish_ts), MAX(publish_ts) FROM articles WHERE ghid=? AND publish_ts>0",
            (ghid,),
        ).fetchone()
        return (as_int(row[0]), as_int(row[1])) if row else (0, 0)

    def articles(
        self,
        ghid: str,
        *,
        recent: int = 0,
        start_ts: int = 0,
        end_ts: int = 0,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [ghid]
        where = ["ghid = ?"]
        if start_ts:
            where.append("publish_ts >= ?")
            params.append(start_ts)
        if end_ts:
            where.append("publish_ts < ?")
            params.append(end_ts)
        sql = f"SELECT * FROM articles WHERE {' AND '.join(where)} ORDER BY publish_ts DESC, article_key"
        if recent:
            sql += " LIMIT ?"
            params.append(recent)
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]


def candidate_identifier(candidate: dict[str, Any]) -> str:
    for field in ("username", "alias"):
        value = str(candidate.get(field) or "").strip()
        if GHID_PATTERN.fullmatch(value):
            return value
    return ""


def candidate_account_name(candidate: dict[str, Any]) -> str:
    return str(candidate.get("accountName") or candidate.get("account_name") or "")


def public_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values = []
    for position, item in enumerate(items, 1):
        if not candidate_identifier(item):
            continue
        values.append(
            {
                "index": as_int(item.get("position"), position),
                "accountName": candidate_account_name(item),
                "alias": str(item.get("alias") or ""),
                "description": str(item.get("description") or ""),
                "verification": str(item.get("verification") or ""),
                "latestUpdate": str(item.get("latestUpdate") or item.get("latest_update") or ""),
            }
        )
    return values


def select_candidate(
    index: ArchiveIndex,
    account: str,
    items: list[dict[str, Any]],
    requested_position: int,
) -> dict[str, Any] | None:
    candidates = [item for item in items if candidate_identifier(item)]
    chosen: dict[str, Any] | None = None
    if requested_position:
        chosen = next((item for item in candidates if as_int(item.get("position")) == requested_position), None)
        if chosen is None and 1 <= requested_position <= len(candidates):
            chosen = candidates[requested_position - 1]
        if chosen is None:
            raise ManggeError("候选编号不存在，请从返回的候选中选择")
    else:
        exact = [item for item in candidates if normalize_name(candidate_account_name(item)) == normalize_name(account)]
        if len(exact) == 1:
            chosen = exact[0]
    if chosen:
        ghid = candidate_identifier(chosen)
        index.save_mapping(account, chosen, ghid)
        return index.mapping(account)
    return None


def determine_mode(args: argparse.Namespace) -> tuple[str, int, int, int]:
    if args.all:
        if args.start_date or args.end_date:
            raise ManggeError("--all 不能与日期范围同时使用")
        return "all", 0, 0, 0
    if args.start_date or args.end_date:
        if not args.start_date or not args.end_date:
            raise ManggeError("日期范围必须同时提供 --start-date 和 --end-date")
        start_ts, end_ts = parse_date_window(args.start_date, args.end_date)
        return "range", 0, start_ts, end_ts
    recent = args.recent or 1
    if recent < 1:
        raise ManggeError("最近文章数量至少为 1")
    return "recent", recent, 0, 0


def state_path_for(args: argparse.Namespace) -> Path:
    if args.state_file:
        return Path(args.state_file).expanduser()
    return Path(args.output_dir).expanduser() / "_mangge_state.sqlite3"


def scope_is_complete(index: ArchiveIndex, ghid: str, mode: str, start_ts: int, end_ts: int) -> bool:
    checkpoint = index.checkpoint(ghid)
    if mode == "all":
        return bool(checkpoint and not as_bool(checkpoint.get("has_more")))
    if mode == "range":
        minimum, maximum = index.coverage(ghid)
        if checkpoint and not as_bool(checkpoint.get("has_more")):
            return maximum >= end_ts or maximum > 0
        return minimum > 0 and minimum <= start_ts and maximum >= end_ts
    return False


def plan_calls(
    index: ArchiveIndex,
    args: argparse.Namespace,
    products: dict[str, Product],
    mode: str,
    recent: int,
    start_ts: int,
    end_ts: int,
) -> dict[str, Any]:
    mapping = index.mapping(args.account) if args.account else None
    resolved_ghid = args.ghid or (str(mapping.get("ghid")) if mapping else "")
    needs_search = bool(args.account and not resolved_ghid)
    if args.candidate and args.account and not resolved_ghid:
        cached = index.search_results(args.account)
        if cached:
            mapping = select_candidate(index, args.account, cached, args.candidate)
            resolved_ghid = str(mapping.get("ghid")) if mapping else ""
            needs_search = not bool(resolved_ghid)

    complete = bool(resolved_ghid and scope_is_complete(index, resolved_ghid, mode, start_ts, end_ts))
    if mode == "recent":
        history_calls = min(args.max_pages, math.ceil(recent / PAGE_SIZE))
    else:
        history_calls = 0 if complete else args.max_pages

    cached_missing = 0
    if resolved_ghid and not args.metadata_only:
        rows = index.articles(
            resolved_ghid,
            recent=recent if mode == "recent" else 0,
            start_ts=start_ts if mode == "range" else 0,
            end_ts=end_ts if mode == "range" else 0,
        )
        cached_missing = sum(row["body_status"] != "ok" and bool(row["article_url"]) for row in rows)

    if args.metadata_only:
        body_calls = 0
    elif mode == "recent":
        body_calls = min(recent, args.max_articles)
    else:
        body_calls = min(args.max_articles, cached_missing + history_calls * PAGE_SIZE)

    requests = [
        (SEARCH_SLUG, 1 if needs_search else 0, {"query": args.account, "sort": "latest", "limit": 10}),
        (HISTORY_SLUG, history_calls, {"ghid": resolved_ghid or "planned", "page": 1}),
        (CONTENT_SLUG, body_calls, {"url": "https://mp.weixin.qq.com/s?planned", "format": "text"}),
    ]
    breakdown = []
    total = 0
    for slug, count, payload in requests:
        unit = products[slug].estimate_micros(payload)
        subtotal = unit * count
        total += subtotal
        breakdown.append(
            {
                "slug": slug,
                "count": count,
                "unit_price_micros": unit,
                "subtotal_micros": subtotal,
            }
        )
    return {
        "estimated_max_cost_micros": total,
        "estimated_max_cost_yuan": micros_to_yuan_text(total),
        "breakdown": breakdown,
        "needs_account_search": needs_search,
        "history_pages_max": history_calls,
        "body_calls_max": body_calls,
        "scope_already_complete": complete,
    }


def paid_call(
    client: ManggeClient,
    ledger: CostLedger,
    product: Product,
    payload: dict[str, Any],
) -> dict[str, Any]:
    ledger.authorize(product, payload)
    response, actual = client.call(product, payload)
    ledger.record(product, actual)
    return response


def account_from_mapping(args: argparse.Namespace, mapping: dict[str, Any] | None) -> tuple[str, str]:
    if args.ghid:
        if not GHID_PATTERN.fullmatch(args.ghid):
            raise ManggeError("提供的公众号标识不符合公开接口格式")
        return args.ghid, args.account or args.ghid
    if mapping:
        return str(mapping["ghid"]), str(mapping.get("account_name") or args.account)
    return "", args.account


def resolve_account(
    index: ArchiveIndex,
    client: ManggeClient,
    ledger: CostLedger,
    products: dict[str, Product],
    args: argparse.Namespace,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    mapping = index.mapping(args.account) if args.account else None
    if mapping or args.ghid:
        return mapping, []
    cached = index.search_results(args.account)
    if args.candidate and cached:
        mapping = select_candidate(index, args.account, cached, args.candidate)
        return mapping, []

    payload = {"query": args.account, "sort": "latest", "limit": 10}
    response = paid_call(client, ledger, products[SEARCH_SLUG], payload)
    data = response.get("data") or {}
    items = data.get("items") if isinstance(data, dict) else []
    items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    index.save_search(args.account, items)
    cached = index.search_results(args.account)
    mapping = select_candidate(index, args.account, cached, args.candidate)
    return mapping, public_candidates(cached) if not mapping else []


def history_start(index: ArchiveIndex, ghid: str, mode: str) -> tuple[int, str]:
    if mode == "recent":
        return 1, ""
    checkpoint = index.checkpoint(ghid)
    if checkpoint and as_bool(checkpoint.get("has_more")):
        collection_id = str(checkpoint.get("collection_id") or "")
        next_page = as_int(checkpoint.get("next_page"), 0)
        if collection_id and next_page >= 1:
            return next_page, collection_id
    return 1, ""


def scan_history(
    index: ArchiveIndex,
    client: ManggeClient,
    ledger: CostLedger,
    product: Product,
    ghid: str,
    account_name: str,
    mode: str,
    recent: int,
    start_ts: int,
    end_ts: int,
    max_pages: int,
) -> tuple[int, bool, str]:
    if mode != "recent" and scope_is_complete(index, ghid, mode, start_ts, end_ts):
        return 0, True, ""
    page, collection_id = history_start(index, ghid, mode)
    pages_used = 0
    seen_fingerprints: set[str] = set()
    reason = ""
    complete = False
    while pages_used < max_pages:
        request_body: dict[str, Any] = {"ghid": ghid, "page": page}
        if page > 1:
            if not collection_id:
                raise ManggeError("续采缺少 collectionId，拒绝冷跳页")
            request_body["collectionId"] = collection_id
        try:
            response = paid_call(client, ledger, product, request_body)
        except ManggeError as exc:
            index.save_error_checkpoint(ghid, exc.payload)
            raise
        data = response.get("data") or {}
        if not isinstance(data, dict):
            raise ManggeError("历史文章响应缺少 data 对象")
        items = data.get("items")
        if not isinstance(items, list):
            raise ManggeError("历史文章响应缺少 items 数组")
        items = [item for item in items if isinstance(item, dict)]
        response_collection = str(data.get("collectionId") or collection_id)
        if not response_collection:
            raise ManggeError("历史文章响应缺少 collectionId")
        response_page = as_int(data.get("page"), page)
        fingerprint = index.save_page(ghid, account_name, response_collection, response_page, items)
        if fingerprint in seen_fingerprints:
            reason = "repeated_page"
            break
        seen_fingerprints.add(fingerprint)
        has_more = as_bool(data.get("hasMore"))
        next_page_raw = data.get("nextPage")
        next_page = as_int(next_page_raw, 0) if next_page_raw is not None else 0
        index.save_checkpoint(ghid, response_collection, next_page or None, has_more)
        pages_used += 1
        collection_id = response_collection

        if mode == "recent" and len(index.articles(ghid, recent=recent)) >= recent:
            complete = True
            break
        if not has_more:
            complete = True
            break
        if mode == "range" and scope_is_complete(index, ghid, mode, start_ts, end_ts):
            complete = True
            break
        if next_page <= page:
            reason = "invalid_next_page"
            break
        page = next_page
    if not complete and not reason:
        reason = "page_limit_reached"
    return pages_used, complete, reason


def selected_articles(
    index: ArchiveIndex,
    ghid: str,
    mode: str,
    recent: int,
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    if mode == "recent":
        return index.articles(ghid, recent=recent)
    if mode == "range":
        return index.articles(ghid, start_ts=start_ts, end_ts=end_ts)
    return index.articles(ghid)


def write_archive(
    output_root: Path,
    account_name: str,
    rows: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> Path:
    account_dir = output_root / sanitize_filename(account_name)
    article_dir = account_dir / "articles"
    article_dir.mkdir(parents=True, exist_ok=True)
    index_rows = []
    for row in rows:
        if row.get("body_status") != "ok":
            continue
        date_text = "unknown-date"
        if as_int(row.get("publish_ts")) > 0:
            date_text = dt.datetime.fromtimestamp(as_int(row["publish_ts"]), SHANGHAI_TZ).date().isoformat()
        filename = f"{date_text}_{sanitize_filename(str(row.get('title') or '未命名'))}_{str(row['article_key'])[:8]}.md"
        relative = Path("articles") / filename
        body = str(row.get("body_text") or "").strip()
        document = (
            f"# {row.get('title') or '未命名'}\n\n"
            f"- 公众号：{row.get('account_name') or account_name}\n"
            f"- 发布时间：{row.get('publish_time') or ''}\n"
            f"- 原文：{row.get('article_url') or ''}\n\n"
            f"{body}\n"
        )
        atomic_write(account_dir / relative, document)
        index_rows.append(
            {
                "title": row.get("title") or "",
                "publishTime": row.get("publish_time") or "",
                "url": row.get("article_url") or "",
                "file": relative.as_posix(),
            }
        )
    atomic_write(account_dir / "index.json", json.dumps(index_rows, ensure_ascii=False, indent=2) + "\n")
    atomic_write(account_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return account_dir


def run_status(args: argparse.Namespace) -> int:
    try:
        api_key = load_api_key(args)
    except ManggeError:
        emit_json({"status": "not_configured", "configured": False})
        return 1
    if not args.verify:
        emit_json({"status": "configured", "configured": True})
        return 0
    try:
        ManggeClient(api_key, args.timeout).product(CONTENT_SLUG)
    except ManggeError as exc:
        emit_json({"status": "invalid", "configured": True, "valid": False, "error": str(exc)})
        return 2
    emit_json({"status": "ready", "configured": True, "valid": True})
    return 0


def run_setup(args: argparse.Namespace) -> int:
    path = config_file_for(args)
    if path.exists() and not args.overwrite:
        raise ManggeError("本机已存在配置；如需替换，请明确使用 --overwrite")
    api_key = getpass.getpass("曼格云 API Key（输入不会显示）: ").strip()
    if not api_key:
        raise ManggeError("API Key 不能为空")
    client = ManggeClient(api_key, args.timeout)
    client.product(CONTENT_SLUG)
    write_api_key(path, api_key)
    emit_json({"status": "saved", "configured": True})
    return 0


def run_collect(args: argparse.Namespace) -> int:
    if not args.account and not args.ghid:
        raise ManggeError("请提供公众号名称")
    if args.max_pages < 1:
        raise ManggeError("--max-pages 至少为 1")
    if args.max_articles < 0:
        raise ManggeError("--max-articles 不能为负数")
    mode, recent, start_ts, end_ts = determine_mode(args)
    if mode == "recent" and math.ceil(recent / PAGE_SIZE) > args.max_pages:
        raise ManggeError("最近文章数量超过本轮页数上限；请提高 --max-pages")

    api_key = load_api_key(args)
    client = ManggeClient(api_key, args.timeout)
    products = client.products()
    state_path = state_path_for(args)

    with ArchiveIndex(state_path) as index:
        plan = plan_calls(index, args, products, mode, recent, start_ts, end_ts)
        if args.dry_run:
            emit_json(
                {
                    "status": "dry_run",
                    "mode": mode,
                    "account": args.account or args.ghid,
                    **plan,
                },
                args.output_file,
            )
            return 0
        if not args.confirm_paid:
            emit_json(
                {
                    "status": "confirmation_required",
                    "mode": mode,
                    "account": args.account or args.ghid,
                    **plan,
                },
                args.output_file,
            )
            return 3
        max_micros = yuan_to_micros(args.max_cost)
        if plan["estimated_max_cost_micros"] > max_micros:
            raise ManggeError(
                f"实时预估 ¥{plan['estimated_max_cost_yuan']} 超过已确认上限 ¥{micros_to_yuan_text(max_micros)}"
            )
        ledger = CostLedger(max_micros=max_micros)

        mapping, candidates = resolve_account(index, client, ledger, products, args)
        if candidates:
            emit_json(
                {
                    "status": "needs_selection",
                    "account": args.account,
                    "candidates": candidates,
                    "actual_cost_micros": ledger.spent_micros,
                    "actual_cost_yuan": micros_to_yuan_text(ledger.spent_micros),
                },
                args.output_file,
            )
            return 0

        ghid, account_name = account_from_mapping(args, mapping)
        if not ghid:
            raise ManggeError("未能从搜索结果中获得历史接口支持的公众号标识")
        pages_used = 0
        metadata_complete = False
        reason = ""
        try:
            pages_used, metadata_complete, reason = scan_history(
                index,
                client,
                ledger,
                products[HISTORY_SLUG],
                ghid,
                account_name,
                mode,
                recent,
                start_ts,
                end_ts,
                args.max_pages,
            )
        except ManggeError as exc:
            rows = selected_articles(index, ghid, mode, recent, start_ts, end_ts)
            emit_json(
                {
                    "status": "partial",
                    "reason": "ambiguous_network_failure" if exc.ambiguous else "history_failed",
                    "error": str(exc),
                    "account": account_name,
                    "metadata_count": len(rows),
                    "pages_used": pages_used,
                    "actual_cost_micros": ledger.spent_micros,
                    "actual_cost_yuan": micros_to_yuan_text(ledger.spent_micros),
                    "resume_saved": True,
                },
                args.output_file,
            )
            return 2

        rows = selected_articles(index, ghid, mode, recent, start_ts, end_ts)
        missing = [row for row in rows if row["body_status"] != "ok" and row["article_url"]]
        missing_unavailable = sum(row["body_status"] != "ok" and not row["article_url"] for row in rows)
        exact_body_micros = sum(
            products[CONTENT_SLUG].estimate_micros({"url": row["article_url"], "format": "text"})
            for row in missing
        )
        if args.metadata_only or (mode in {"all", "range"} and not metadata_complete):
            status = "metadata_ready" if metadata_complete else "partial"
            emit_json(
                {
                    "status": status,
                    "reason": reason,
                    "account": account_name,
                    "mode": mode,
                    "metadata_count": len(rows),
                    "missing_body_count": len(missing),
                    "unavailable_body_count": missing_unavailable,
                    "estimated_body_cost_micros": exact_body_micros,
                    "estimated_body_cost_yuan": micros_to_yuan_text(exact_body_micros),
                    "pages_used": pages_used,
                    "has_more": not metadata_complete,
                    "actual_cost_micros": ledger.spent_micros,
                    "actual_cost_yuan": micros_to_yuan_text(ledger.spent_micros),
                    "resume_saved": True,
                },
                args.output_file,
            )
            return 0

        body_error = ""
        for row in missing[: args.max_articles]:
            payload = {"url": row["article_url"], "format": "text"}
            try:
                response = paid_call(client, ledger, products[CONTENT_SLUG], payload)
                data = response.get("data") or {}
                content = data.get("content") if isinstance(data, dict) else ""
                if not isinstance(content, str) or not content.strip():
                    raise ManggeError("正文接口返回空内容")
                article = data.get("article") if isinstance(data.get("article"), dict) else {}
                index.save_body(row["article_key"], content.strip(), article)
            except ManggeError as exc:
                body_error = str(exc)
                reason = "ambiguous_network_failure" if exc.ambiguous else "body_failed"
                break

        final_rows = selected_articles(index, ghid, mode, recent, start_ts, end_ts)
        remaining = [row for row in final_rows if row["body_status"] != "ok" and row["article_url"]]
        ready = [row for row in final_rows if row["body_status"] == "ok"]
        if remaining and not reason:
            reason = "body_limit_reached"
        status = "success" if metadata_complete and not remaining and not body_error else "partial"
        manifest = {
            "status": status,
            "reason": reason,
            "generatedAt": dt.datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
            "account": account_name,
            "mode": mode,
            "metadataCount": len(final_rows),
            "articleCount": len(ready),
            "remainingBodyCount": len(remaining),
            "actualCostMicros": ledger.spent_micros,
            "actualCostYuan": micros_to_yuan_text(ledger.spent_micros),
        }
        output_dir = write_archive(Path(args.output_dir).expanduser(), account_name, ready, manifest)
        emit_json(
            {
                "status": status,
                "reason": reason,
                "error": body_error,
                "account": account_name,
                "mode": mode,
                "metadata_count": len(final_rows),
                "article_count": len(ready),
                "remaining_body_count": len(remaining),
                "unavailable_body_count": missing_unavailable,
                "pages_used": pages_used,
                "has_more": not metadata_complete,
                "actual_cost_micros": ledger.spent_micros,
                "actual_cost_yuan": micros_to_yuan_text(ledger.spent_micros),
                "output_dir": str(output_dir),
                "resume_saved": True,
            },
            args.output_file,
        )
        return 0 if status == "success" else 2


def add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config-file", default="", help="覆盖默认本地 .env 位置")
    parser.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过曼格云按公众号名称归档微信文章正文")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="检查是否已配置曼格云 API Key")
    add_config_args(status)
    status.add_argument("--verify", action="store_true", help="通过免费产品目录验证密钥")

    setup = subparsers.add_parser("setup", help="通过本机隐藏输入保存 API Key")
    add_config_args(setup)
    setup.add_argument("--overwrite", action="store_true", help="覆盖已有本地配置")

    collect = subparsers.add_parser("collect", help="按名称采集最近或历史文章正文")
    add_config_args(collect)
    collect.add_argument("--account", default="", help="公众号名称")
    collect.add_argument("--ghid", default="", help=argparse.SUPPRESS)
    collect.add_argument("--candidate", type=int, default=0, help="选择上次搜索返回的候选编号")
    collect.add_argument("--recent", type=int, default=0, help="最近文章篇数；未指定范围时默认为 1")
    collect.add_argument("--all", action="store_true", help="采集全部历史；按 max-pages 分批")
    collect.add_argument("--start-date", default="", help="北京时间开始日期 YYYY-MM-DD")
    collect.add_argument("--end-date", default="", help="北京时间结束日期 YYYY-MM-DD，包含当天")
    collect.add_argument("--metadata-only", action="store_true", help="只采文章目录，不购买正文")
    collect.add_argument("--max-pages", type=int, default=10, help="本轮最多购买的历史页数")
    collect.add_argument("--max-articles", type=int, default=200, help="本轮最多购买的正文篇数")
    collect.add_argument("--dry-run", action="store_true", help="免费查询实时价格并输出费用上限")
    collect.add_argument("--confirm-paid", action="store_true", help="已获得用户对范围和金额的明确确认")
    collect.add_argument("--max-cost", default="0", help="本轮允许的最高人民币金额")
    collect.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="本地归档目录")
    collect.add_argument("--state-file", default="", help="覆盖默认断点数据库位置")
    collect.add_argument("--output-file", default="", help="可选：同步保存 JSON 运行摘要")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "status":
            return run_status(args)
        if args.command == "setup":
            return run_setup(args)
        if args.command == "collect":
            return run_collect(args)
        raise ManggeError("未知命令")
    except ManggeError as exc:
        emit_json(
            {
                "status": "partial" if exc.ambiguous else "failed",
                "code": exc.code,
                "error": str(exc),
            },
            getattr(args, "output_file", ""),
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
