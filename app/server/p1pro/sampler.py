"""采集线程。

设备本身**不保存任何历史**，曲线完全依赖这里持续采样并落库，
所以这一层要同时照顾三件事：采样密度、设备承受能力、以及失败后的退避。

采样节奏：

* 正常：每 ``interval_seconds``（默认 2 秒）采一次，两个请求（状态 + 电量），
  单次往返约 34 ms，占空比不到 4%。
* 连续失败：指数退避 2→4→8…最多 ``backoff_max_seconds``，避免插座掉线时
  每秒都在等一次 TCP 超时，把线程和日志一起拖死。
* 恢复瞬间补一条 "链路恢复" 事件，方便事后对着曲线解释那段空白。
"""

from __future__ import annotations

import threading
import time

from .protocol import PlugError


class Sampler:
    def __init__(self, config, client, store, on_change=None):
        self.config = config
        self.client = client
        self.store = store
        self._on_change = on_change
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._suppress_until = 0.0
        self._state = {
            "online": False,
            "ts": 0,
            "power_w": None,
            "voltage_v": None,
            "current_a": None,
            "sockets": [0, 0, 0, 0],
            "bitmap": 0,
            "led": 0,
            "def_st": 0,
            "error": "尚未连接设备",
            "failures": 0,
            "latency_ms": 0.0,
            "samples": 0,
            "started_at": 0,
        }

    # ------------------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="p1pro-sampler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread:
            thread.join(timeout=3.0)

    def alive(self):
        return bool(self._thread and self._thread.is_alive())

    def suppress_state_event(self, seconds=6.0):
        """面板主动下发控制后调用，避免同一次动作被采两遍。"""
        with self._lock:
            self._suppress_until = time.time() + seconds

    def snapshot(self):
        with self._lock:
            data = dict(self._state)
            data["sockets"] = list(self._state["sockets"])
            data["sampler_alive"] = self.alive()
            data["interval_seconds"] = self.config.interval
            data["age_seconds"] = int(time.time() - data["ts"]) if data["ts"] else None
            return data

    # ------------------------------------------------------------------

    def _set(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)

    def _run(self):
        self._set(started_at=int(time.time()))
        failures = 0
        while not self._stop.is_set():
            interval = self.config.interval
            started = time.time()
            try:
                status = self.client.get_status()
                electric = self.client.get_electric()
            except PlugError as exc:
                failures += 1
                self._handle_failure(exc, failures)
                delay = min(interval * (2 ** min(failures, 6)), float(self.config.get("sampler.backoff_max_seconds", 60)))
                self._sleep(max(interval, delay))
                continue

            failures = 0
            now = int(time.time())

            old = self._state
            was_online = bool(old["online"])
            old_bitmap = int(old["bitmap"])

            p_mw = max(0, electric["power_mw"])
            u_mv = max(0, electric["vol_mv"])
            i_ua = max(0, electric["curr_ua"])

            self._set(
                online=True,
                ts=now,
                power_w=round(p_mw / 1000.0, 3),
                voltage_v=round(u_mv / 1000.0, 2),
                current_a=round(i_ua / 1e6, 4),
                sockets=status["sockets"],
                bitmap=status["bitmap"],
                led=status["led"],
                def_st=status["def_st"],
                error="",
                failures=0,
                latency_ms=round(self.client.stats.get("last_ms", 0.0), 1),
                samples=int(old["samples"]) + 1,
            )

            try:
                self.store.insert_sample(now, p_mw, u_mv, i_ua, status["bitmap"])
                # 每分钟收一次桶。没跨分钟时 resolve 内部只是比较游标，开销可忽略。
                self.store.finalize(now)
            except Exception as exc:  # 磁盘写失败不该杀掉采集线程
                self._set(error="写入数据库失败: %s" % exc)

            if not was_online:
                self.store.add_event("link", "链路恢复，恢复采样（间隔 %ds）" % interval, now)
            if was_online and old_bitmap != status["bitmap"]:
                if time.time() >= self._suppress_until:
                    self.store.add_event(
                        "switch",
                        "状态变化 %s → %s" % (_bits(old_bitmap), _bits(status["bitmap"])),
                        now,
                    )

            if self._on_change:
                try:
                    self._on_change(self.snapshot())
                except Exception:
                    pass

            elapsed = time.time() - started
            self._sleep(max(0.05, interval - elapsed))

    def _handle_failure(self, exc, failures):
        was_online = bool(self._state["online"])
        self._set(
            online=False,
            failures=failures,
            error=exc.message if hasattr(exc, "message") else str(exc),
            sockets=[0, 0, 0, 0],
        )
        if was_online or failures == 1:
            self.store.add_event(
                "link",
                "采样失败：%s（连续 %d 次）" % (getattr(exc, "message", str(exc)), failures),
            )

    def _sleep(self, seconds):
        # 用 wait 而不是 sleep，停机时能立刻退出
        self._stop.wait(max(0.0, float(seconds)))


def _bits(bitmap):
    return "".join("1" if (bitmap >> i) & 1 else "0" for i in range(4))
