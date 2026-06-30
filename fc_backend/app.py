
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque

from flask import Flask, jsonify, request


app = Flask(__name__)

FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
FEISHU_RECORD_URL = (
    "https://open.feishu.cn/open-apis/bitable/v1/apps/"
    "{base_token}/tables/{table_id}/records"
)
DEFAULT_ORIGINS = "https://rewqasd.github.io,http://127.0.0.1:8765,http://localhost:8765"
ALLOWED_ORIGINS = {
    item.strip()
    for item in os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")
    if item.strip()
}

BUDGET_ALIASES = {
    "150以下": "150以下",
    "150-250": "150–250",
    "150–250": "150–250",
    "250-350": "250–350",
    "250–350": "250–350",
    "350以上": "350以上",
}
PHONES = {"Mate 80", "Pura 90 Pro", "X9 Ultra", "X300s", "Find N6", "Mate 80 RS", "其他"}
FOCUSES = {"影像拍摄", "商务办公", "续航耐用", "综合体验"}
SERVICES = {"", "分期方案", "机型对比", "到企体验", "暂不确定"}
TIMES = {"", "午休", "下班后", "均可"}

_token_lock = threading.Lock()
_token_cache = {"value": "", "expires_at": 0}
_request_log = defaultdict(deque)


def _json_response(payload, status=200, origin=None):
    response = jsonify(payload)
    response.status_code = status
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Max-Age"] = "600"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _post_json(url, payload, headers=None, timeout=8):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"upstream HTTP {exc.code}: {detail[:500]}") from exc


def _tenant_access_token():
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]

    with _token_lock:
        now = time.time()
        if _token_cache["value"] and now < _token_cache["expires_at"]:
            return _token_cache["value"]

        app_id = os.getenv("FEISHU_APP_ID", "")
        app_secret = os.getenv("FEISHU_APP_SECRET", "")
        if not app_id or not app_secret:
            raise RuntimeError("Feishu credentials are not configured")

        result = _post_json(
            FEISHU_TOKEN_URL,
            {"app_id": app_id, "app_secret": app_secret},
        )
        if result.get("code") != 0 or not result.get("tenant_access_token"):
            raise RuntimeError(f"Feishu token error: {result.get('code')}")

        _token_cache["value"] = result["tenant_access_token"]
        _token_cache["expires_at"] = now + max(60, int(result.get("expire", 7200)) - 300)
        return _token_cache["value"]


def _clean_text(value, max_length):
    text = str(value or "").strip()
    return text[:max_length]


def _limited(client_ip):
    now = time.time()
    bucket = _request_log[client_ip]
    while bucket and now - bucket[0] > 300:
        bucket.popleft()
    if len(bucket) >= 3:
        return True
    bucket.append(now)
    return False


def _validate_payload(payload):
    name = _clean_text(payload.get("name"), 30)
    phone = re.sub(r"\s+", "", _clean_text(payload.get("phone"), 20))
    budget = BUDGET_ALIASES.get(_clean_text(payload.get("budget"), 20), "")
    focus = _clean_text(payload.get("focus"), 20)
    service = _clean_text(payload.get("serviceNeed"), 20)
    preferred_time = _clean_text(payload.get("time"), 20)
    note = _clean_text(payload.get("note"), 200)
    other_phone = _clean_text(payload.get("otherPhone"), 60)
    wanted = payload.get("wantedPhone", [])
    if isinstance(wanted, str):
        wanted = [wanted]
    wanted = [_clean_text(item, 30) for item in wanted if _clean_text(item, 30)]

    if not 2 <= len(name) <= 30:
        return None, "请输入正确的姓名"
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        return None, "请输入正确的 11 位手机号"
    if not budget:
        return None, "请选择月度预算"
    if not wanted or any(item not in PHONES for item in wanted):
        return None, "请选择期望手机"
    if "其他" in wanted and not other_phone:
        return None, "请填写其他期望机型"
    if focus not in FOCUSES:
        return None, "请选择最关注的终端体验"
    if service not in SERVICES or preferred_time not in TIMES:
        return None, "提交内容包含无效选项"

    if other_phone:
        note = f"{note}\n其他期望机型：{other_phone}".strip()

    fields = {
        "姓名": name,
        "手机号": phone,
        "期望月度预算": budget,
        "期望手机": wanted,
        "最关注的终端体验": focus,
    }
    if service:
        fields["希望了解的服务"] = service
    if preferred_time:
        fields["希望时段"] = preferred_time
    if note:
        fields["备注"] = note
    return fields, ""


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "OPTIONS"])
@app.route("/<path:path>", methods=["GET", "POST", "OPTIONS"])
def entry(path):
    origin = request.headers.get("Origin", "")

    if request.method == "OPTIONS":
        if origin not in ALLOWED_ORIGINS:
            return _json_response({"ok": False}, 403)
        return _json_response({"ok": True}, 204, origin)

    if request.method == "GET":
        return _json_response({"ok": True, "service": "employee-benefits-feishu-api"}, 200, origin)

    if path.strip("/") != "submit":
        return _json_response({"ok": False, "message": "Not found"}, 404, origin)
    if origin not in ALLOWED_ORIGINS:
        return _json_response({"ok": False, "message": "请求来源无效"}, 403)
    if request.content_length and request.content_length > 16384:
        return _json_response({"ok": False, "message": "提交内容过大"}, 413, origin)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _json_response({"ok": False, "message": "提交格式无效"}, 400, origin)
    if _clean_text(payload.get("company_website"), 100):
        return _json_response({"ok": True}, 200, origin)

    client_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )
    if _limited(client_ip):
        return _json_response({"ok": False, "message": "提交过于频繁，请稍后再试"}, 429, origin)

    fields, error = _validate_payload(payload)
    if error:
        return _json_response({"ok": False, "message": error}, 400, origin)

    base_token = os.getenv("FEISHU_BASE_TOKEN", "")
    table_id = os.getenv("FEISHU_TABLE_ID", "")
    if not base_token or not table_id:
        return _json_response({"ok": False, "message": "服务尚未完成配置"}, 503, origin)

    try:
        token = _tenant_access_token()
        result = _post_json(
            FEISHU_RECORD_URL.format(base_token=base_token, table_id=table_id),
            {"fields": fields},
            headers={"Authorization": f"Bearer {token}"},
        )
        if result.get("code") != 0:
            raise RuntimeError(f"Feishu record error: {result.get('code')}")
        return _json_response({"ok": True, "message": "提交成功"}, 200, origin)
    except Exception as exc:
        app.logger.exception("Form submission failed: %s", exc)
        return _json_response(
            {"ok": False, "message": "提交暂时未成功，请稍后再试"},
            502,
            origin,
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000)
