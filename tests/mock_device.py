#!/usr/bin/env python3
"""模拟一台 P1Pro 插线板，监听 6767 协议。

用途：
  1. 本地开发时没有插座也能调界面
  2. CI 里做端到端冒烟测试（不需要真机）

    python3 tests/mock_device.py --port 16767

行为尽量贴近真机：同样的 key 校验（`time` 不校验时效）、同样的返回码、
同样的"没定时时连 timer 字段都不返回"、同样的单连接语义（用锁串行）。
功率会随时间缓慢波动，这样曲线能画出形状。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SN = "000000000001"
SALT = "==smart-plug=="
LOCK = threading.Lock()


class Device:
    def __init__(self):
        self.sockets = [1, 1, 0, 1]
        self.led = 1
        self.def_st = 2
        self.name = "PLUG-P1Pro-TEST"
        self.timers = {i: [] for i in range(4)}
        self.countdowns = {}
        self.started = time.time()

    def electric(self):
        elapsed = time.time() - self.started
        wave = math.sin(elapsed / 90.0) * 16000 + math.sin(elapsed / 17.0) * 4000
        power_mw = max(2000, int(42000 + wave + random.randint(-1200, 1200)))
        vol_mv = 229000 + random.randint(-1200, 1200)
        # 视在电流按功率因数 0.75 反算，和真机一样不能靠 P=UI 互推
        curr_ua = int(power_mw / 0.75 / (vol_mv / 1000.0) * 1000)
        return {"vol": vol_mv, "curr": curr_ua, "power": power_mw, "result": 0}

    def status(self):
        return {
            "response": [{"index": i, "status": v} for i, v in enumerate(self.sockets)],
            "led": self.led,
            "def_st": self.def_st,
            "result": 0,
        }

    def countdown_view(self, index):
        cd = self.countdowns.get(index)
        if not cd:
            return {"count": 0, "remain": 0, "action": 255, "result": 11}
        remain = max(0, int(cd["end_at"] - time.time()))
        if remain <= 0:
            # 到点执行并清除
            self.sockets[index] = cd["action"]
            self.countdowns.pop(index, None)
            return {"count": 0, "remain": 0, "action": 255, "result": 11}
        return {"count": cd["count"], "remain": remain, "action": cd["action"], "result": 0}


DEV = Device()


def require_int(params, key):
    try:
        return int(params[key][0])
    except (KeyError, IndexError, ValueError):
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "esp8266-httpd/0.3"

    def log_message(self, fmt, *args):
        pass

    def _reply(self, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._handle()

    def do_PUT(self):
        # 真机对 PUT 直接断连
        self.close_connection = True

    def _handle(self):
        parsed = urlparse(self.path)
        if parsed.path != "/plug":
            self._reply({"result": 1})
            return
        params = parse_qs(parsed.query)
        method = (params.get("_api") or [""])[0]

        if method == "get_plug_sn":
            return self._reply({"sn": SN, "type": "P1Pro", "result": 0})

        # 鉴权：key 必须匹配，time 不校验时效（与真机一致）
        stamp = (params.get("time") or [""])[0]
        key = (params.get("key") or [""])[0]
        expect = hashlib.md5((SN + SALT + stamp).encode()).hexdigest()
        if not stamp or key != expect:
            return self._reply({"result": 3})

        with LOCK:
            result = self._dispatch(method, params)
        self._reply(result)

    def _dispatch(self, method, params):
        d = DEV

        if method == "get_plug_status":
            return d.status()
        if method == "get_plug_electric":
            return d.electric()
        if method == "get_plug_info":
            return {"name": d.name, "sn": SN, "mac": "02:00:00:00:00:01",
                    "version": "2.0.4", "led": d.led, "def_st": d.def_st, "result": 0}
        if method == "get_plug_version":
            return {"version": "2.0.4", "version_num": 204, "develop_num": 0, "result": 0}
        if method == "get_plug_wifi":
            return {"ip": "127.0.0.1", "mask": "255.255.255.0", "gw": "127.0.0.1",
                    "ssid": "MOCK", "password": "", "result": 0}
        if method == "get_plug_name":
            return {"name": d.name, "result": 0}
        if method == "plug_upgrade_status":
            return {"rate": 0, "status": 0, "result": 0}

        if method == "plug_timer_get":
            index = require_int(params, "index")
            if index is None:
                return {"result": 4}
            if not 0 <= index < 4:
                return {"result": 5}
            timers = d.timers[index]
            if not timers:
                return {"result": 0}          # 关键：没有 timer 字段
            return {"timer": [dict(t) for t in timers], "result": 0}

        if method == "plug_cntdown_get":
            index = require_int(params, "index")
            if index is None:
                return {"result": 4}
            if not 0 <= index < 4:
                return {"result": 5}
            return d.countdown_view(index)

        if method == "set_plug_status":
            index = require_int(params, "index")
            status = require_int(params, "status")
            if index is None or status is None:
                return {"result": 4}
            if not 0 <= index < 4 or status not in (0, 1):
                return {"result": 5}
            d.sockets[index] = status
            return {"result": 0}

        if method == "set_all_plug_status":
            status = require_int(params, "status")
            if status is None:
                return {"result": 4}
            if status not in (0, 1):
                return {"result": 5}
            d.sockets = [status] * 4
            return {"result": 0}

        if method == "set_plug_led":
            enabled = require_int(params, "enabled")
            if enabled is None:
                return {"result": 4}
            d.led = 1 if enabled else 0
            return {"result": 0}

        if method == "set_plug_dfltstat":
            value = require_int(params, "default")
            if value is None:
                return {"result": 4}
            if value not in (0, 2):
                return {"result": 5}
            d.def_st = 2 if value == 2 else 1
            return {"result": 0}

        if method == "set_plug_name":
            name = (params.get("name") or [""])[0]
            if not name:
                return {"result": 4}
            d.name = name[:32]
            return {"result": 0}

        if method == "plug_timer_add":
            index = require_int(params, "index")
            raw = (params.get("timer") or [""])[0]
            if index is None or not raw:
                return {"result": 4}
            if not 0 <= index < 4:
                return {"result": 5}
            try:
                timer = json.loads(raw)
                item = {
                    "time": int(timer["time"]) % 1440,
                    "action": 1 if int(timer.get("action", 1)) else 0,
                    "repeat": int(timer.get("repeat", 0)) & 0x7F,
                    "enabled": 1,     # 固件强制改成 1
                }
            except (ValueError, KeyError, TypeError):
                return {"result": 9}
            d.timers[index].append(item)
            return {"result": 0}

        if method == "plug_timer_del":
            index = require_int(params, "index")
            when = require_int(params, "timer")
            repeat = require_int(params, "repeat")
            if index is None or when is None or repeat is None:
                return {"result": 4}
            if not 0 <= index < 4:
                return {"result": 5}
            before = len(d.timers[index])
            d.timers[index] = [t for t in d.timers[index]
                               if not (t["time"] == when and t["repeat"] == repeat)]
            if len(d.timers[index]) == before:
                return {"result": 9}
            return {"result": 0}

        if method == "plug_timer_set":
            return {"result": 9}          # 真机就是恒 9

        if method == "plug_cntdown_add":
            index = require_int(params, "index")
            action = require_int(params, "action")
            count = require_int(params, "count")
            if index is None or action is None or count is None:
                return {"result": 4}
            if not 0 <= index < 4:
                return {"result": 5}
            d.countdowns[index] = {
                "count": count,
                "action": 1 if action else 0,
                "end_at": time.time() + max(1, count),
            }
            return {"result": 0}

        if method == "plug_cntdown_del":
            index = require_int(params, "index")
            if index is None:
                return {"result": 4}
            if not 0 <= index < 4:
                return {"result": 5}
            if index not in d.countdowns:
                return {"result": 11}
            d.countdowns.pop(index, None)
            return {"result": 0}

        return {"result": 1}


def main():
    parser = argparse.ArgumentParser(description="P1Pro 模拟设备")
    parser.add_argument("--port", type=int, default=16767)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    httpd.daemon_threads = True
    print("mock P1Pro listening on %s:%d (sn=%s)" % (args.bind, args.port, SN), flush=True)
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
