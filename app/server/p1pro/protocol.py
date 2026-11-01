"""P1Pro 本地 6767 协议客户端。

这个文件是整个项目的信任边界：所有对插座的读写都从这里出去。

相对旧版（OpenWrt 插件里的 C 实现）修掉的三个隐患：

* **静态盐 ``01010000``** → 改回实时 ``MMddHHmm``。旧版注释说是"省掉 MD5 开销"，
  实际一次 MD5 不到 10 µs，而一次 TCP 往返是 34 ms，省下的是 0.03%；
  代价却是整套逻辑依赖"固件不校验时间"这个缺陷。
* **鉴权失败不自愈** → ``result=3`` 时清空 SN 缓存、重走发现流程、重试一次。
  换插座 / 插座恢复出厂后不会永久离线。
* **并发打单连接设备** → 全局可重入锁串行化。ESP8266 同一时刻只接受一个连接。

另外补充了旧版完全没有的能力：设备原生定时 / 倒计时 / 设备名 / 断电记忆。
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ---- 设备返回码（实测，不是文档抄来的） ----
RESULT_OK = 0
RESULT_UNKNOWN_METHOD = 1
RESULT_AUTH_FAILED = 3
RESULT_MISSING_PARAM = 4
RESULT_BAD_INDEX = 5        # 索引越界，或取值非法（status=7 也返回 5）
RESULT_CONFLICT = 9         # 目标不存在 / 参数冲突
RESULT_NO_COUNTDOWN = 11    # 该口没有倒计时

# 传输层自造的错误码（设备不会返回负数）
ERR_NETWORK = -1
ERR_TIMEOUT = -2
ERR_BAD_JSON = -3
ERR_OFFLINE = -4

RESULT_TEXT = {
    RESULT_OK: "成功",
    RESULT_UNKNOWN_METHOD: "固件不支持该方法",
    RESULT_AUTH_FAILED: "鉴权失败（key 不匹配）",
    RESULT_MISSING_PARAM: "缺少必需参数",
    RESULT_BAD_INDEX: "插口索引越界或取值非法",
    RESULT_CONFLICT: "目标不存在或参数冲突",
    RESULT_NO_COUNTDOWN: "该插口没有倒计时",
    ERR_NETWORK: "网络不可达",
    ERR_TIMEOUT: "请求超时",
    ERR_BAD_JSON: "设备返回了非 JSON 内容",
    ERR_OFFLINE: "尚未连接设备",
}

SALT = "==smart-plug=="

# ESP8266 的 httpd 处理完一个请求后需要一点时间复位。
# 实测连续打四个请求（设备信息面板）时，第二三个会被 RST（WinError 10054），
# 加 35 ms 间隔后 100% 稳定。
MIN_GAP_SECONDS = 0.035

# 只有只读方法才允许传输层重试。
# 写方法不能重试——`plug_timer_add` 重试会凭空多出一条定时。
READ_METHODS = frozenset([
    "get_plug_sn", "get_plug_status", "get_plug_electric", "get_plug_info",
    "get_plug_version", "get_plug_wifi", "get_plug_name", "plug_upgrade_status",
    "plug_timer_get", "plug_cntdown_get",
])

# repeat 位图：bit0=周日 … bit6=周六
REPEAT_EVERYDAY = 127
REPEAT_WEEKDAY = 62
REPEAT_WEEKEND = 65
REPEAT_ONCE = 0


class PlugError(Exception):
    """设备返回错误码，或传输层失败。"""

    def __init__(self, code, message=None, method=None):
        self.code = int(code)
        self.method = method
        self.message = message or RESULT_TEXT.get(self.code, "未知错误(%s)" % code)
        super().__init__("[%s] %s%s" % (self.code, self.message, (" @ " + method) if method else ""))

    @property
    def transport(self):
        return self.code < 0

    def to_dict(self):
        return {"ok": False, "code": self.code, "message": self.message, "method": self.method}


class PlugClient:
    """单实例、线程安全、串行化的 P1Pro 客户端。"""

    def __init__(self, host, port=6767, timeout=3.0):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._lock = threading.RLock()
        # 必须绕开系统代理：本机开着代理软件时，urllib 会把 192.168.x.x 也丢给代理
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._sn = ""
        self._sn_at = 0.0
        self._last_req_at = 0.0
        self.last_error = ""
        self.stats = {"calls": 0, "errors": 0, "last_ok_at": 0.0, "last_ms": 0.0}

    # ------------------------------------------------------------------
    # 底层
    # ------------------------------------------------------------------

    def configure(self, host=None, port=None, timeout=None):
        with self._lock:
            if host is not None and str(host) != self.host:
                self.host = str(host)
                self._sn = ""
                self._sn_at = 0.0
            if port is not None:
                self.port = int(port)
            if timeout is not None:
                self.timeout = float(timeout)

    def _throttle(self):
        if self._last_req_at:
            wait = MIN_GAP_SECONDS - (time.time() - self._last_req_at)
            if wait > 0:
                time.sleep(wait)
        self._last_req_at = time.time()

    def _http(self, query):
        url = "http://%s:%d/plug?%s" % (self.host, self.port, query)
        started = time.time()
        try:
            with self._opener.open(url, timeout=self.timeout) as resp:
                body = resp.read()
        except socket.timeout:
            raise PlugError(ERR_TIMEOUT, "请求超时 %.1fs" % self.timeout)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, socket.timeout) or "timed out" in str(reason):
                raise PlugError(ERR_TIMEOUT, "请求超时 %.1fs" % self.timeout)
            raise PlugError(ERR_NETWORK, "网络不可达: %s" % reason)
        except (ConnectionError, OSError) as exc:
            raise PlugError(ERR_NETWORK, "网络不可达: %s" % exc)
        self.stats["last_ms"] = (time.time() - started) * 1000.0

        text = body.decode("utf-8", "replace").strip()
        if not text:
            raise PlugError(ERR_BAD_JSON, "设备返回空响应")
        try:
            data = json.loads(text)
        except ValueError:
            raise PlugError(ERR_BAD_JSON, "非 JSON 响应: %r" % text[:120])
        if not isinstance(data, dict):
            raise PlugError(ERR_BAD_JSON, "响应不是对象: %r" % text[:120])
        return data

    @staticmethod
    def _sign(sn, salt_time=None):
        stamp = salt_time or time.strftime("%m%d%H%M")
        key = hashlib.md5((sn + SALT + stamp).encode("utf-8")).hexdigest()
        return stamp, key

    def _ensure_sn(self):
        if self._sn:
            return self._sn
        data = self._http("_api=get_plug_sn")
        sn = str(data.get("sn") or "").strip()
        if not sn:
            raise PlugError(ERR_BAD_JSON, "get_plug_sn 未返回 sn: %r" % data)
        self._sn = sn
        self._sn_at = time.time()
        return sn

    def forget_sn(self):
        with self._lock:
            self._sn = ""
            self._sn_at = 0.0

    # ------------------------------------------------------------------
    # 统一调用入口
    # ------------------------------------------------------------------

    def call(self, method, params=None, _retry_auth=True):
        params = params or {}
        with self._lock:  # 单连接设备，必须串行
            self.stats["calls"] += 1
            try:
                return self._call_locked(method, params, _retry_auth)
            except PlugError as exc:
                self.stats["errors"] += 1
                self.last_error = exc.message
                raise

    def _call_locked(self, method, params, retry_auth):
        pairs = []
        if method != "get_plug_sn":
            sn = self._ensure_sn()
            stamp, key = self._sign(sn)
            pairs.append(("time", stamp))
            pairs.append(("key", key))
        for name, value in params.items():
            pairs.append((name, value))
        pairs.append(("_api", method))
        query = urllib.parse.urlencode(pairs)

        self._throttle()
        try:
            data = self._http(query)
        except PlugError as exc:
            # 只读请求允许一次传输层重试（设备偶发 RST），写请求绝不重试
            if exc.transport and method in READ_METHODS:
                time.sleep(0.12)
                self._throttle()
                data = self._http(query)
            else:
                raise
        code = data.get("result")
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = ERR_BAD_JSON

        if code == RESULT_AUTH_FAILED and retry_auth and method != "get_plug_sn":
            # 自愈：SN 变了（换插座 / 恢复出厂）或缓存脏了，重走一次发现
            self.forget_sn()
            return self._call_locked(method, params, False)

        if code != RESULT_OK:
            raise PlugError(code, method=method)
        self.stats["last_ok_at"] = time.time()
        self.last_error = ""
        return data

    # ------------------------------------------------------------------
    # 只读
    # ------------------------------------------------------------------

    def get_status(self):
        data = self.call("get_plug_status")
        sockets = [0, 0, 0, 0]
        for item in data.get("response") or []:
            try:
                idx = int(item.get("index", -1))
                if 0 <= idx < 4:
                    sockets[idx] = 1 if int(item.get("status", 0)) else 0
            except (TypeError, ValueError):
                continue
        return {
            "sockets": sockets,
            # 位图形式，和云端 get-status-logs 的 state 字段同构
            "bitmap": sum(v << i for i, v in enumerate(sockets)),
            "led": int(data.get("led", 1)),
            "def_st": int(data.get("def_st", 0)),
        }

    def get_electric(self):
        data = self.call("get_plug_electric")
        return {
            "power_mw": _to_int(data.get("power")),
            "vol_mv": _to_int(data.get("vol")),
            "curr_ua": _to_int(data.get("curr")),
        }

    def get_info(self):
        data = self.call("get_plug_info")
        return {
            "name": str(data.get("name") or ""),
            "sn": str(data.get("sn") or ""),
            "mac": str(data.get("mac") or ""),
            "version": str(data.get("version") or ""),
            "led": int(data.get("led", 0) or 0),
            "def_st": int(data.get("def_st", 0) or 0),
        }

    def get_wifi(self):
        data = self.call("get_plug_wifi")
        return {
            "ip": str(data.get("ip") or ""),
            "mask": str(data.get("mask") or ""),
            "gw": str(data.get("gw") or ""),
            "ssid": str(data.get("ssid") or ""),
        }

    def get_version(self):
        data = self.call("get_plug_version")
        return {
            "version": str(data.get("version") or ""),
            "version_num": _to_int(data.get("version_num")),
            "develop_num": _to_int(data.get("develop_num")),
        }

    def get_upgrade_status(self):
        data = self.call("plug_upgrade_status")
        return {"rate": _to_int(data.get("rate")), "status": _to_int(data.get("status"))}

    def list_timers(self, index):
        """读某口的定时列表。

        坑：没有定时时固件返回 ``{"result":0}``，**连 timer 字段都没有**。
        只看 result 会误判成"有定时"，必须判断字段是否存在。
        """
        data = self.call("plug_timer_get", {"index": int(index)})
        raw = data.get("timer")
        out = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                try:
                    out.append({
                        "time": int(item.get("time", 0)),
                        "action": int(item.get("action", 0)),
                        "repeat": int(item.get("repeat", 0)),
                        "enabled": int(item.get("enabled", 1)),
                    })
                except (TypeError, ValueError):
                    continue
        out.sort(key=lambda t: (t["time"], t["repeat"]))
        return out

    def get_countdown(self, index):
        """读倒计时。空态返回 result=11 且 action=255，这里归一化成 countdown=None。"""
        try:
            data = self.call("plug_cntdown_get", {"index": int(index)})
        except PlugError as exc:
            if exc.code == RESULT_NO_COUNTDOWN:
                return None
            raise
        action = _to_int(data.get("action"))
        if action not in (0, 1):
            return None
        return {
            "count": _to_int(data.get("count")),
            "remain": _to_int(data.get("remain")),
            "action": action,
        }

    # ------------------------------------------------------------------
    # 写 / 动作
    # ------------------------------------------------------------------

    def set_switch(self, index, on):
        return self.call("set_plug_status", {"index": int(index), "status": 1 if on else 0})

    def set_switch_all(self, on):
        return self.call("set_all_plug_status", {"status": 1 if on else 0})

    def set_led(self, enabled):
        return self.call("set_plug_led", {"enabled": 1 if enabled else 0})

    def set_default_state(self, on):
        """断电记忆。注意取值是 0 / 2，不是布尔——传 1 无效。"""
        return self.call("set_plug_dfltstat", {"default": 2 if on else 0})

    def set_device_name(self, name):
        """改整机设备名（≤32 字符），真写进插座，官方 App 同步可见。"""
        return self.call("set_plug_name", {"name": str(name)[:32]})

    def add_timer(self, index, time_min, action, repeat):
        """新增定时。

        ``time`` 是 **UTC 0 点起的分钟数**（不是本地时间）；
        ``repeat`` 是星期位图；``enabled`` 写入时会被固件强制改成 1。
        """
        timer = {
            "time": int(time_min) % 1440,
            "action": 1 if action else 0,
            "repeat": int(repeat) & 0x7F,
            "enabled": 1,
        }
        payload = json.dumps(timer, separators=(",", ":"))
        return self.call("plug_timer_add", {"index": int(index), "timer": payload})

    def del_timer(self, index, time_min, repeat):
        return self.call("plug_timer_del", {
            "index": int(index),
            "timer": int(time_min) % 1440,
            "repeat": int(repeat) & 0x7F,
        })

    def add_countdown(self, index, action, seconds):
        return self.call("plug_cntdown_add", {
            "index": int(index),
            "action": 1 if action else 0,
            "count": max(1, int(seconds)),
        })

    def del_countdown(self, index):
        try:
            return self.call("plug_cntdown_del", {"index": int(index)})
        except PlugError as exc:
            if exc.code == RESULT_NO_COUNTDOWN:
                return {"result": 0}
            raise

    # ------------------------------------------------------------------
    # 便捷
    # ------------------------------------------------------------------

    def snapshot(self):
        """一次采样：状态 + 电量。两个请求，串行发出。"""
        status = self.get_status()
        electric = self.get_electric()
        return {"status": status, "electric": electric}

    def ping(self):
        try:
            self._http("_api=get_plug_sn")
            return True
        except PlugError:
            return False


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------------
# 网络发现
# ----------------------------------------------------------------------

def probe_host(ip, port=6767, timeout=0.6):
    """TCP 探活 + 取 SN。不是 P1Pro 就返回 None。"""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            pass
    except (OSError, socket.timeout):
        return None
    client = PlugClient(ip, port, timeout=timeout)
    try:
        data = client.call("get_plug_sn")
    except PlugError:
        return None
    sn = str(data.get("sn") or "")
    if not sn:
        return None
    return {"ip": ip, "sn": sn, "type": str(data.get("type") or "")}


def discover_subnet(cidr, port=6767, timeout=0.45, workers=64):
    """扫一个 /24，找所有 6767 端口上的 P1Pro。约 3~5 秒。"""
    import ipaddress
    from concurrent.futures import ThreadPoolExecutor

    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return []
    targets = [str(ip) for ip in net.hosts()]
    if len(targets) > 1024:
        targets = targets[:1024]
    found = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(lambda ip: probe_host(ip, port, timeout), targets):
            if result:
                found.append(result)
    return found
