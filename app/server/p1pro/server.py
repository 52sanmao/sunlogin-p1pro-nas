"""HTTP 服务：静态页 + JSON API。

不用 Flask / FastAPI，只用标准库 ``http.server``。理由很实际：
飞牛 NAS 上装 Python 包要动 pip、要管 venv、升级系统还可能把依赖冲掉；
而这两个模块哪儿都有。整个项目 ``import`` 到的第三方包数量是 **0**。
"""

from __future__ import annotations

import csv
import hmac
import io
import json
import mimetypes
import os
import posixpath
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .protocol import (
    ERR_OFFLINE,
    RESULT_TEXT,
    PlugError,
    PlugClient,
    discover_subnet,
)
from .store import RANGES, Store

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

LEVEL_LABEL = {"min": "分钟", "hour": "小时", "day": "天"}


def utc_offset_minutes(ts=None):
    """本机相对 UTC 的偏移分钟数（东八区 = 480）。

    Windows 的 ``struct_time`` 没有 ``tm_gmtoff``，所以只能用 timezone/daylight 推。
    """
    ts = time.time() if ts is None else ts
    try:
        lt = time.localtime(ts)
        offset = -(time.altzone if (lt.tm_isdst and time.daylight) else time.timezone)
    except (ValueError, OverflowError):
        offset = 0
    return int(offset // 60)


class App:
    def __init__(self, config, store, client, sampler):
        self.config = config
        self.store = store
        self.client = client
        self.sampler = sampler
        self.started_at = int(time.time())
        self._device_cache = {"ts": 0, "data": None}
        self._discover_lock = __import__("threading").Lock()

    # ------------------------------------------------------------------
    # 设备信息（带缓存，避免每屏都打 4 个请求）
    # ------------------------------------------------------------------

    def device_info(self, force=False):
        now = time.time()
        cached = self._device_cache
        if not force and cached["data"] and now - cached["ts"] < 15:
            return cached["data"]
        data = {
            "info": None,
            "wifi": None,
            "version": None,
            "upgrade": None,
            "error": "",
            "reachable": False,
        }
        try:
            data["info"] = self.client.get_info()
            data["reachable"] = True
        except PlugError as exc:
            data["error"] = exc.message
            self._device_cache = {"ts": now, "data": data}
            return data
        for key, fn in (("wifi", self.client.get_wifi),
                        ("version", self.client.get_version),
                        ("upgrade", self.client.get_upgrade_status)):
            try:
                data[key] = fn()
            except PlugError as exc:
                data[key] = None
                data["error"] = data["error"] or exc.message
        self._device_cache = {"ts": now, "data": data}
        return data

    def invalidate_device(self):
        self._device_cache = {"ts": 0, "data": None}

    # ------------------------------------------------------------------
    # 配置视图
    # ------------------------------------------------------------------

    def config_view(self):
        return {
            "price_per_kwh": self.config.price,
            "interval_seconds": self.config.interval,
            "socket_labels": self.config.socket_labels,
            "rated_watts": int(self.config.get("limits.rated_watts", 2500)),
            "host": self.config.host,
            "port": self.config.port,
            "raw_retention_days": int(self.config.get("storage.raw_retention_days", 7)),
            "minute_retention_days": int(self.config.get("storage.minute_retention_days", 400)),
            "default_range": str(self.config.get("ui.default_range", "24h")),
            "auth_enabled": bool(str(self.config.get("web.access_token") or "").strip()),
        }

    def bootstrap(self):
        return {
            "app": {
                "name": "P1Pro 控制台（飞牛版）",
                "version": __version__,
                "started_at": self.started_at,
                "uptime_seconds": int(time.time()) - self.started_at,
            },
            "config": self.config_view(),
            "state": self.sampler.snapshot(),
            "summary": self.store.summary(),
            "storage": self.store.stats(),
            "tz_offset_minutes": utc_offset_minutes(),
            "server_now": int(time.time()),
        }

    # ------------------------------------------------------------------
    # 定时 / 倒计时
    # ------------------------------------------------------------------

    def local_to_device_minutes(self, hour, minute):
        """本地时间 → 设备要的 UTC 分钟数。

        这是整个项目最容易写错的一处：``time`` 字段是 UTC 0 点起的分钟数，
        北京时间 07:30 必须填 1410 而不是 450。填 450 不会报错，只是永远不触发。
        """
        local = (int(hour) % 24) * 60 + (int(minute) % 60)
        return (local - utc_offset_minutes()) % 1440

    def device_to_local_minutes(self, device_minutes):
        return (int(device_minutes) + utc_offset_minutes()) % 1440

    def all_timers(self):
        offset = utc_offset_minutes()
        out = []
        for index in range(4):
            entry = {"index": index, "timers": [], "error": ""}
            try:
                for item in self.client.list_timers(index):
                    local = (int(item["time"]) + offset) % 1440
                    entry["timers"].append({
                        "time_utc": item["time"],
                        "hour": local // 60,
                        "minute": local % 60,
                        "action": item["action"],
                        "repeat": item["repeat"],
                        "enabled": item["enabled"],
                        "days": decode_repeat(item["repeat"]),
                    })
            except PlugError as exc:
                entry["error"] = exc.message
            out.append(entry)
        return {"sockets": out, "tz_offset_minutes": offset}

    def all_countdowns(self):
        out = []
        for index in range(4):
            entry = {"index": index, "countdown": None, "error": ""}
            try:
                entry["countdown"] = self.client.get_countdown(index)
            except PlugError as exc:
                entry["error"] = exc.message
            out.append(entry)
        return {"sockets": out}


REPEAT_NAMES = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]


def decode_repeat(repeat):
    repeat = int(repeat) & 0x7F
    return {
        "value": repeat,
        "bits": [(repeat >> i) & 1 for i in range(7)],
        "names": [REPEAT_NAMES[i] for i in range(7) if (repeat >> i) & 1],
        "text": repeat_text(repeat),
    }


def repeat_text(repeat):
    if repeat == 0:
        return "仅一次"
    if repeat == 127:
        return "每天"
    if repeat == 62:
        return "工作日"
    if repeat == 65:
        return "周末"
    names = [REPEAT_NAMES[i] for i in range(7) if (repeat >> i) & 1]
    return "、".join(names) if names else "仅一次"


# ======================================================================
# HTTP 层
# ======================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "P1ProNAS/" + __version__
    protocol_version = "HTTP/1.1"
    app = None  # 由 serve() 注入

    # ---------------- 基础 ----------------

    def log_message(self, fmt, *args):
        if os.environ.get("P1PRO_QUIET"):
            return
        print("[web] %s %s" % (self.address_string(), fmt % args), flush=True)

    def _send(self, status, body, content_type="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False), extra={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                                       "style-src 'self' 'unsafe-inline'; "
                                       "script-src 'self'; connect-src 'self'; "
                                       "frame-ancestors 'none'",
        })

    def _fail(self, message, status=400, code=None):
        payload = {"ok": False, "error": message}
        if code is not None:
            payload["code"] = code
        self._json(payload, status)

    def _ok(self, payload=None):
        data = {"ok": True}
        if payload:
            data.update(payload)
        return data

    # ---------------- 鉴权 ----------------

    def _token_ok(self, query):
        expected = str(self.app.config.get("web.access_token") or "").strip()
        if not expected:
            return True
        provided = (query.get("token", [""])[0]
                    or self.headers.get("X-P1Pro-Token", "")
                    or self._cookie_token())
        return bool(provided) and hmac.compare_digest(str(provided), expected)

    def _cookie_token(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            if "=" in part:
                key, _, value = part.partition("=")
                if key.strip() == "p1pro_token":
                    return urllib.parse.unquote(value.strip())
        return ""

    # ---------------- 请求分派 ----------------

    def _parse(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        return path, query

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, query = self._parse()
        try:
            if path.startswith("/api/"):
                if path == "/api/health":
                    return self._json({"ok": True, "version": __version__})
                if not self._token_ok(query):
                    return self._fail("需要访问口令", 401)
                return self._api_get(path, query)
            return self._static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # 任何未捕获异常都别让服务挂掉
            self._fail("服务端错误：%s" % exc, 500)

    def do_POST(self):
        path, query = self._parse()
        try:
            if not self._token_ok(query):
                return self._fail("需要访问口令", 401)
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1024 * 1024:
                return self._fail("请求体过大", 413)
            raw = self.rfile.read(length) if length else b""
            body = {}
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    return self._fail("请求体不是合法 JSON")
                if not isinstance(body, dict):
                    return self._fail("请求体必须是对象")
            return self._api_post(path, body, query)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._fail("服务端错误：%s" % exc, 500)

    def do_DELETE(self):
        self.do_POST()

    # ---------------- 静态文件 ----------------

    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        if path == "/favicon.ico":
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                '<rect width="32" height="32" rx="8" fill="#0b0f19"/>'
                '<circle cx="16" cy="16" r="6" fill="none" stroke="#ff6b00" stroke-width="2.6"/>'
                "</svg>"
            )
            return self._send(200, svg, "image/svg+xml")
        # 防路径穿越
        safe = posixpath.normpath(path).lstrip("/")
        if safe.startswith("..") or ".." in safe.split("/"):
            return self._fail("非法路径", 403)
        full = os.path.join(STATIC_DIR, safe.replace("/", os.sep))
        if not os.path.isfile(full):
            return self._fail("未找到", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            body = fh.read()
        cache = "no-cache" if full.endswith(".html") else "public, max-age=300"
        self._send(200, body, ctype, extra={"Cache-Control": cache,
                                            "Content-Security-Policy":
                                            "default-src 'self'; img-src 'self' data:; "
                                            "style-src 'self' 'unsafe-inline'; "
                                            "script-src 'self'; connect-src 'self'"})

    # ---------------- GET ----------------

    def _api_get(self, path, query):
        app = self.app
        if path == "/api/bootstrap":
            return self._json(app.bootstrap())
        if path == "/api/state":
            return self._json(app.sampler.snapshot())
        if path == "/api/summary":
            return self._json(app.store.summary())
        if path == "/api/series":
            name = (query.get("range", ["24h"])[0] or "24h").strip()
            if name not in RANGES:
                return self._fail("不支持的时间范围：%s" % name)
            return self._json(app.store.series(name))
        if path == "/api/device":
            return self._json(app.device_info(force=query.get("force", ["0"])[0] == "1"))
        if path == "/api/timers":
            return self._json(app.all_timers())
        if path == "/api/countdowns":
            return self._json(app.all_countdowns())
        if path == "/api/events":
            limit = min(500, max(1, int(query.get("limit", ["60"])[0] or 60)))
            return self._json({"events": app.store.events(limit)})
        if path == "/api/export":
            return self._export(query)
        if path == "/api/storage":
            return self._json(app.store.stats())
        return self._fail("未知接口：%s" % path, 404)

    # ---------------- POST ----------------

    def _api_post(self, path, body, query):
        app = self.app
        client = app.client

        if path == "/api/switch":
            scope = str(body.get("scope", "all"))
            status = 1 if int(body.get("status", 0)) else 0
            app.sampler.suppress_state_event()
            if scope == "all":
                client.set_switch_all(status)
                app.store.add_event("switch", "面板：全部%s" % ("开启" if status else "关闭"))
                msg = "已全部%s" % ("开启" if status else "关闭")
            else:
                index = int(body.get("index", -1))
                if not 0 <= index < 4:
                    return self._fail("插口索引越界", 400, 5)
                client.set_switch(index, status)
                labels = app.config.socket_labels
                app.store.add_event("switch", "面板：%s %s" % (
                    labels[index], "开启" if status else "关闭"))
                msg = "%s 已%s" % (labels[index], "开启" if status else "关闭")
            app.sampler.suppress_state_event()
            return self._json(self._ok({"message": msg, "state": app.sampler.snapshot()}))

        if path == "/api/switch_many":
            statuses = body.get("statuses")
            if not isinstance(statuses, list) or len(statuses) != 4:
                return self._fail("statuses 必须是长度为 4 的数组")
            name = str(body.get("name") or "自定义")
            app.sampler.suppress_state_event(8.0)
            results = []
            for i, raw in enumerate(statuses):
                try:
                    client.set_switch(i, 1 if int(raw) else 0)
                    results.append({"index": i, "ok": True})
                except PlugError as exc:
                    results.append({"index": i, "ok": False, "error": exc.message})
            app.sampler.suppress_state_event(8.0)
            bits = "".join("1" if int(s) else "0" for s in statuses)
            app.store.add_event("switch", "场景「%s」→ %s" % (name, bits))
            failed = [r for r in results if not r["ok"]]
            if failed:
                return self._json({
                    "ok": False,
                    "error": "部分插口失败：%s" % failed[0]["error"],
                    "results": results,
                }, 200)
            return self._json(self._ok({"message": "已切换到「%s」" % name, "results": results}))

        if path == "/api/led":
            enabled = 1 if int(body.get("enabled", 1)) else 0
            client.set_led(enabled)
            app.store.add_event("switch", "面板：指示灯%s" % ("开启" if enabled else "关闭"))
            return self._json(self._ok({"enabled": enabled}))

        if path == "/api/default_state":
            enabled = 1 if int(body.get("enabled", 0)) else 0
            client.set_default_state(enabled)
            app.store.add_event("system", "断电记忆设为%s" % ("来电自启" if enabled else "保持关闭"))
            app.invalidate_device()
            return self._json(self._ok({"enabled": enabled}))

        if path == "/api/device_name":
            name = str(body.get("name", "")).strip()
            if not name:
                return self._fail("设备名不能为空")
            if len(name) > 32:
                return self._fail("设备名最多 32 个字符")
            client.set_device_name(name)
            app.store.add_event("system", "设备名改为 %s（已写入插座）" % name)
            app.invalidate_device()
            return self._json(self._ok({"name": name}))

        if path == "/api/socket_label":
            index = int(body.get("index", -1))
            if not 0 <= index < 4:
                return self._fail("插口索引越界", 400, 5)
            label = str(body.get("label", "")).strip()[:32]
            labels = app.config.socket_labels
            labels[index] = label or "插口 %d" % (index + 1)
            app.config.set("sockets", labels)
            return self._json(self._ok({"labels": labels}))

        if path == "/api/timer":
            return self._timer(post_body=body)

        if path == "/api/countdown":
            return self._countdown(body)

        if path == "/api/config":
            return self._config(body)

        if path == "/api/discover":
            cidr = str(body.get("cidr", "")).strip()
            if not cidr:
                host = app.config.host
                parts = host.split(".")
                cidr = "%s.%s.%s.0/24" % (parts[0], parts[1], parts[2]) if len(parts) == 4 else "192.168.0.0/24"
            found = discover_subnet(cidr)
            return self._json({"ok": True, "cidr": cidr, "found": found})

        if path == "/api/probe":
            host = str(body.get("host", "")).strip()
            if not host:
                return self._fail("缺少 host")
            try:
                sn = PlugClient(host, app.config.port, 2.0).call("get_plug_sn")
            except PlugError as exc:
                return self._json({"ok": False, "error": exc.message}, 200)
            return self._json({"ok": True, "sn": sn.get("sn"), "type": sn.get("type")})

        return self._fail("未知接口：%s" % path, 404)

    # ---------------- 具体动作 ----------------

    def _timer(self, post_body):
        app = self.app
        client = app.client
        op = str(post_body.get("op", "add"))
        index = int(post_body.get("index", -1))
        if not 0 <= index < 4:
            return self._fail("插口索引越界", 400, 5)

        if op == "add":
            hour = int(post_body.get("hour", 0))
            minute = int(post_body.get("minute", 0))
            action = 1 if int(post_body.get("action", 1)) else 0
            repeat = int(post_body.get("repeat", 127)) & 0x7F
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return self._fail("时间不合法")
            device_time = app.local_to_device_minutes(hour, minute)
            client.add_timer(index, device_time, action, repeat)
            app.store.add_event(
                "timer", "%s：%02d:%02d %s %s 已写入插座" % (
                    app.config.socket_labels[index], hour, minute,
                    "开启" if action else "关闭", repeat_text(repeat)))
            return self._json(self._ok({
                "message": "定时已写入插座（%02d:%02d，%s）" % (hour, minute, repeat_text(repeat)),
                "device_time": device_time,
                "timers": app.all_timers(),
            }))

        if op in ("del", "delete"):
            device_time = int(post_body.get("time_utc", -1))
            repeat = int(post_body.get("repeat", 0)) & 0x7F
            if device_time < 0:
                # 允许传本地时间，服务端换算
                hour = int(post_body.get("hour", 0))
                minute = int(post_body.get("minute", 0))
                device_time = app.local_to_device_minutes(hour, minute)
            client.del_timer(index, device_time, repeat)
            local = app.device_to_local_minutes(device_time)
            app.store.add_event(
                "timer", "%s：删除 %02d:%02d %s" % (
                    app.config.socket_labels[index], local // 60, local % 60,
                    repeat_text(repeat)))
            return self._json(self._ok({
                "message": "定时已删除",
                "timers": app.all_timers(),
            }))

        return self._fail("不支持的定时操作：%s" % op)

    def _countdown(self, body):
        app = self.app
        client = app.client
        op = str(body.get("op", "add"))
        index = int(body.get("index", -1))
        if not 0 <= index < 4:
            return self._fail("插口索引越界", 400, 5)

        if op == "add":
            action = 1 if int(body.get("action", 0)) else 0
            seconds = int(body.get("seconds", 0))
            if not 1 <= seconds <= 86400 * 7:
                return self._fail("倒计时时长需在 1 秒 ~ 7 天之间")
            client.add_countdown(index, action, seconds)
            app.store.add_event(
                "countdown", "%s：%s 倒计时 %s 已写入插座" % (
                    app.config.socket_labels[index],
                    "开启" if action else "关闭", fmt_seconds(seconds)))
            return self._json(self._ok({"message": "倒计时已下发"}))
        if op in ("del", "delete"):
            client.del_countdown(index)
            app.store.add_event("countdown", "%s：取消倒计时" % app.config.socket_labels[index])
            return self._json(self._ok({"message": "倒计时已取消"}))
        return self._fail("不支持的倒计时操作：%s" % op)

    def _config(self, body):
        app = self.app
        changed = []
        if "price_per_kwh" in body:
            try:
                price = float(body["price_per_kwh"])
            except (TypeError, ValueError):
                return self._fail("电价必须是数字")
            if not 0.0 <= price <= 20.0:
                return self._fail("电价超出合理范围（0~20 元/度）")
            app.config.set("billing.price_per_kwh", round(price, 4))
            changed.append("电价")
        if "interval_seconds" in body:
            try:
                interval = int(body["interval_seconds"])
            except (TypeError, ValueError):
                return self._fail("采样间隔必须是整数")
            if not 1 <= interval <= 300:
                return self._fail("采样间隔需在 1~300 秒之间")
            app.config.set("sampler.interval_seconds", interval)
            app.store.interval = interval
            changed.append("采样间隔")
        if "rated_watts" in body:
            try:
                rated = int(body["rated_watts"])
            except (TypeError, ValueError):
                return self._fail("额定功率必须是整数")
            if not 100 <= rated <= 8000:
                return self._fail("额定功率超出范围（100~8000W）")
            app.config.set("limits.rated_watts", rated)
            changed.append("额定功率")
        if "host" in body:
            host = str(body["host"]).strip()
            if not host:
                return self._fail("设备地址不能为空")
            app.config.set("device.host", host)
            app.client.configure(host=host)
            app.invalidate_device()
            changed.append("设备地址")
        if "access_token" in body:
            token = str(body["access_token"] or "").strip()
            if len(token) > 64:
                return self._fail("访问密码不能超过 64 个字符")
            if token and len(token) < 4:
                return self._fail("访问密码至少 4 位")
            app.config.set("web.access_token", token)
            changed.append("访问密码" if token else "关闭访问密码")
        if "default_range" in body:
            value = str(body["default_range"])
            if value not in RANGES:
                return self._fail("不支持的时间范围")
            app.config.set("ui.default_range", value)
            changed.append("默认图表范围")
        return self._json(self._ok({
            "message": ("已保存：" + "、".join(changed)) if changed else "没有改动",
            "config": app.config_view(),
        }))

    # ---------------- 导出 ----------------

    def _export(self, query):
        app = self.app
        kind = (query.get("type", ["minute"])[0] or "minute").strip()
        days = min(400, max(1, int(query.get("days", ["7"])[0] or 7)))
        now = int(time.time())
        start = now - days * 86400
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")

        if kind == "raw":
            writer.writerow(["ts", "datetime_local", "power_w", "voltage_v", "current_a", "state_bitmap"])
            with app.store._lock:
                rows = app.store.conn.execute(
                    "SELECT ts,p,u,i,st FROM sample WHERE ts>=? ORDER BY ts", (start,)
                ).fetchall()
            for row in rows:
                writer.writerow([
                    row["ts"],
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["ts"])),
                    round(row["p"] / 1000.0, 3),
                    round(row["u"] / 1000.0, 2),
                    round(row["i"] / 1e6, 4),
                    row["st"],
                ])
            filename = "p1pro-raw-%dd.csv" % days
        else:
            level = kind if kind in LEVEL_LABEL else "min"
            writer.writerow(["ts", "datetime_local", "avg_power_w", "max_power_w",
                             "min_power_w", "energy_kwh", "samples", "max_gap_seconds"])
            with app.store._lock:
                rows = app.store.conn.execute(
                    "SELECT ts,p_avg,p_max,p_min,energy_wh,n,gap FROM bucket "
                    "WHERE level=? AND ts>=? ORDER BY ts", (level, start)
                ).fetchall()
            for row in rows:
                writer.writerow([
                    row["ts"],
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["ts"])),
                    round(float(row["p_avg"] or 0) / 1000.0, 3),
                    round(int(row["p_max"] or 0) / 1000.0, 3),
                    round(int(row["p_min"] or 0) / 1000.0, 3),
                    round(float(row["energy_wh"] or 0) / 1000.0, 6),
                    int(row["n"] or 0),
                    int(row["gap"] or 0),
                ])
            filename = "p1pro-%s-%dd.csv" % (level, days)

        data = ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM 让 Excel 正确识别中文
        self._send(200, data, "text/csv; charset=utf-8", extra={
            "Content-Disposition": 'attachment; filename="%s"' % filename,
            "Cache-Control": "no-store",
        })


def fmt_seconds(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%d 秒" % seconds
    if seconds < 3600:
        return "%d 分钟" % (seconds // 60)
    if seconds < 86400:
        return "%d 小时 %d 分" % (seconds // 3600, (seconds % 3600) // 60)
    return "%d 天" % (seconds // 86400)


def serve(app, host, port):
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
