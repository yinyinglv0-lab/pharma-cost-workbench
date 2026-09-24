"""Official competition RPA adapter. The only enabled destination is loopback mock.

POST /api/rpa/tasks sends the initial mock WeChat notification. Independent reminders
use the official POST /api/notify/wechat contract, which has no remote idempotency or
query support. Production I/O remains disabled pending authorized integration.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx


TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
ANALYSIS_TYPES = {"月度成本分析", "季度成本分析", "专题分析"}
REMOTE_STATUSES = {"sent", "received", "confirmed", "in_progress", "completed", "overdue"}


class RPAError(RuntimeError):
    """Classified failure; ambiguous means the server may already have accepted."""

    def __init__(self, code: str, message: str, *, ambiguous: bool = False,
                 retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.ambiguous = ambiguous
        self.retryable = retryable
        self.status_code = status_code

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "ambiguous": self.ambiguous,
                "retryable": self.retryable, "http_status": self.status_code}


class RPAPolicyError(RPAError):
    def __init__(self, message: str):
        super().__init__("destination_policy", message)


def validate_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError("task_id只能含英文字母、数字、下划线、连字符，长度1–128")
    return task_id


def validate_payload(payload: Mapping[str, Any]) -> None:
    """Validate the actual official request, independently of draft validation."""
    required = {"task_id", "task_title", "assignee", "source", "priority", "deadline", "created_at"}
    if not isinstance(payload, Mapping) or not required.issubset(payload):
        raise ValueError("官方RPA请求缺少必填字段")
    if set(payload) - required - {"suggestion", "notify_method"}:
        raise ValueError("官方RPA请求含未定义字段")
    validate_task_id(payload["task_id"])
    if not isinstance(payload["task_title"], str) or not payload["task_title"].strip():
        raise ValueError("task_title不能为空")
    assignee, source = payload["assignee"], payload["source"]
    if not isinstance(assignee, Mapping) or set(assignee) - {"name", "department", "role"}:
        raise ValueError("assignee格式错误")
    for key in ("name", "department"):
        if not isinstance(assignee.get(key), str) or not assignee[key].strip():
            raise ValueError(f"assignee.{key}必填")
    if assignee.get("role") is not None and not isinstance(assignee["role"], str):
        raise ValueError("assignee.role必须是文本或null")
    source_keys = {"analysis_type", "analysis_month", "product", "finding"}
    if not isinstance(source, Mapping) or set(source) != source_keys:
        raise ValueError("source字段不完整")
    if any(not isinstance(source[key], str) or not source[key].strip() for key in source_keys):
        raise ValueError("source字段不能为空")
    if source["analysis_type"] not in ANALYSIS_TYPES:
        raise ValueError("analysis_type须为月度成本分析、季度成本分析或专题分析")
    month = source["analysis_month"]
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("analysis_month须为YYYY-MM")
    date.fromisoformat(month + "-01")
    if payload["priority"] not in {"high", "medium", "low"}:
        raise ValueError("priority须为high/medium/low")
    deadline = payload["deadline"]
    if not isinstance(deadline, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline):
        raise ValueError("deadline须为YYYY-MM-DD")
    date.fromisoformat(deadline)
    if not isinstance(payload["created_at"], str):
        raise ValueError("created_at须为带时区ISO8601时间")
    stamp = datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("created_at须包含时区")
    if payload.get("notify_method", "wechat") != "wechat":
        raise ValueError("当前官方mock闭环仅启用wechat模拟")
    if payload.get("suggestion") is not None and not isinstance(payload["suggestion"], str):
        raise ValueError("suggestion须为文本")


@dataclass(frozen=True)
class RPAConfig:
    base_url: str = "http://127.0.0.1:8090"
    mode: str = "mock"
    timeout_seconds: float = 10.0
    production_allowlist: tuple[str, ...] = ()
    mock_loopback_allowlist: tuple[str, ...] = ()


def _origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        if (parsed.username is not None or parsed.password is not None or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment or
                any(char.isspace() for char in url) or "\\" in url):
            raise ValueError
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError
        return parsed.scheme, parsed.hostname.lower(), port
    except (TypeError, ValueError):
        raise RPAPolicyError("RPA地址必须是无凭据、路径、查询或片段的明确origin") from None


class RPAClient:
    """Synchronous bounded adapter; transport injection is for network-free tests.

    A caller cannot inject an already configured httpx.Client, proxy or redirect policy.
    localhost is pinned to 127.0.0.1, so DNS/environment proxy settings cannot redirect it.
    """

    def __init__(self, config: RPAConfig | None = None, *,
                 transport: httpx.BaseTransport | None = None):
        self.config = config or RPAConfig()
        if not 0 < self.config.timeout_seconds <= 30:
            raise ValueError("RPA超时须大于0且不超过30秒")
        origin = _origin(self.config.base_url)
        if self.config.mode == "mock":
            allowed = {("http", "127.0.0.1", 8090), ("http", "localhost", 8090), ("http", "::1", 8090)}
            for url in self.config.mock_loopback_allowlist:
                extra = _origin(url)
                if extra[0] != "http" or extra[1] not in {"127.0.0.1", "localhost", "::1"} or not 1 <= extra[2] <= 65535:
                    raise RPAPolicyError("mock白名单只能包含明确HTTP loopback origin")
                allowed.add(extra)
            if origin not in allowed:
                raise RPAPolicyError("mock只允许本机8090或明确配置的HTTP loopback白名单")
            host = "[::1]" if origin[1] == "::1" else "127.0.0.1"
            self.base_url = f"http://{host}:{origin[2]}"
        elif self.config.mode == "production":
            allowed = {_origin(url) for url in self.config.production_allowlist}
            if origin[0] != "https" or origin not in allowed:
                raise RPAPolicyError("生产配置须显式指定HTTPS origin allowlist")
            self.base_url = self.config.base_url.rstrip("/")
        else:
            raise RPAPolicyError("RPA模式必须为mock或production")
        self._http = httpx.Client(transport=transport, timeout=self.config.timeout_seconds,
                                  trust_env=False, follow_redirects=False)

    @property
    def simulated(self) -> bool:
        return self.config.mode == "mock"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RPAClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def ensure_enabled(self) -> None:
        if self.config.mode != "mock":
            raise RPAPolicyError("生产RPA网络操作尚未授权，当前实现禁止生产发送和查询")

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None, *, notification=False) -> dict | None:
        self.ensure_enabled()
        posting = method == "POST"
        try:
            response = self._http.request(method, self.base_url + path, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise RPAError("connection_failed", "无法连接官方mock，等待有限重试", retryable=True) from None
        except httpx.TransportError:
            raise RPAError("transport_unknown", "请求传输中断，必须按task_id核对结果",
                           ambiguous=posting, retryable=True) from None
        if 300 <= response.status_code < 400:
            raise RPAPolicyError("RPA重定向被拒绝，未访问重定向目标")
        if not posting and response.status_code == 404:
            return None
        if response.status_code >= 400:
            # Decode JSON first: escaped Unicode and UTF-8 represent the same
            # official duplicate-400 detail and must follow the same recovery.
            detail = response.text[:2000]
            try:
                error_body = response.json() if len(response.content) <= 1_000_000 else {}
                if isinstance(error_body, dict):
                    detail = str(error_body.get("detail", error_body.get("message", detail)))[:2000]
            except ValueError:
                pass
            duplicate = posting and response.status_code in (400, 409) and any(
                token in detail.lower() for token in ("已存在", "duplicate", "already exists"))
            if duplicate:
                raise RPAError("duplicate_id", "官方mock报告重复task_id，须查询并核对已批准内容",
                               ambiguous=True, retryable=True, status_code=response.status_code)
            retryable = response.status_code in (408, 425, 429) or response.status_code >= 500
            raise RPAError("http_error", f"官方mock返回HTTP {response.status_code}",
                           ambiguous=posting and (response.status_code >= 500 or response.status_code == 408),
                           retryable=retryable, status_code=response.status_code)
        try:
            if len(response.content) > 1_000_000:
                raise ValueError("response too large")
            envelope = response.json()
            data = envelope["data"]
            if envelope.get("code") != 200 or not isinstance(data, dict):
                raise ValueError("invalid envelope")
            if notification:
                if (response.status_code != 200 or data.get("status") != "delivered"
                        or data.get("recipient") != payload["recipient"]
                        or not isinstance(data.get("message_id"), str) or not data["message_id"].strip()
                        or not isinstance(data.get("sent_at"), str)):
                    raise ValueError("invalid notification receipt")
                datetime.fromisoformat(data["sent_at"])
            else:
                if data.get("status") not in REMOTE_STATUSES:
                    raise ValueError("invalid status")
                validate_task_id(data.get("task_id"))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RPAError("invalid_response", "官方mock响应格式无效，不能据此认定发送成功",
                           ambiguous=posting, retryable=True) from None
        return data

    def create_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        validate_payload(payload)
        data = self._request("POST", "/api/rpa/tasks", payload)
        if data["task_id"] != payload["task_id"]:
            raise RPAError("receipt_id_mismatch", "回执task_id不匹配，等待按原task_id核对", ambiguous=True)
        return data

    def notify_wechat(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Official mock notification only; an ambiguous response must not be retried."""
        if not isinstance(payload, Mapping) or set(payload) != {"recipient", "department", "message"}:
            raise ValueError("模拟催办请求须包含recipient/department/message")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 8000 for value in payload.values()):
            raise ValueError("模拟催办接收人、部门和消息不能为空")
        return self._request("POST", "/api/notify/wechat", payload, notification=True)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        validate_task_id(task_id)
        data = self._request("GET", "/api/rpa/tasks/" + task_id)
        if data is not None and data["task_id"] != task_id:
            raise RPAError("receipt_id_mismatch", "查询回执task_id不匹配", retryable=True)
        return data


def receipt_matches(payload: Mapping[str, Any], receipt: Mapping[str, Any]) -> bool:
    """ID alone is insufficient after duplicate-400/timeout/DB restoration.

    The actual official mock GET returns the full original body. Partial receipts
    cannot establish identity and are intentionally left unresolved.
    """
    for key in ("task_id", "task_title", "source", "priority", "deadline", "created_at"):
        if receipt.get(key) != payload.get(key):
            return False
    if receipt.get("suggestion") != payload.get("suggestion"):
        return False
    if receipt.get("notify_method", "wechat") != payload.get("notify_method", "wechat"):
        return False
    assignee = receipt.get("assignee")
    if not isinstance(assignee, Mapping):
        return False
    return all((assignee.get(key) or None) == (payload["assignee"].get(key) or None)
               for key in ("name", "department", "role"))
