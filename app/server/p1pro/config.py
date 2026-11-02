"""配置读写。

两个刻意的设计：

1. **原子写**。先写 ``*.tmp`` 再 ``os.replace``，断电不会留下半截 JSON
   （旧版 OpenWrt 插件直接 ``fopen("w")``，掉电可能把配置写成空文件）。
2. **深路径访问**。``cfg.get("sampler.interval_seconds")``，读的时候带类型兜底，
   配置文件被手工改坏也不会让进程起不来。
"""

from __future__ import annotations

import copy
import json
import os
import threading

DEFAULT_CONFIG = {
    "device": {
        "host": "192.168.0.15",
        "port": 6767,
        "timeout": 3.0,
    },
    "sampler": {
        "enabled": True,
        # 2 秒一条：本地直连单次往返约 34 ms，2 秒间隔占用率不到 4%，
        # 对 ESP8266 单连接设备是安全水位。想更密可调 1，想省资源可调 5~10。
        "interval_seconds": 2,
        "backoff_max_seconds": 60,
    },
    "storage": {
        "db_path": "data/p1pro.db",
        # 原始样本只留 7 天（2 秒一条 ≈ 30 万条/天），
        # 更长的历史由分钟 / 小时 / 天三档聚合承担。
        "raw_retention_days": 7,
        "minute_retention_days": 400,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 9787,
        # 留空表示不鉴权。填了之后所有 /api/* 需要带 token
        # （浏览器端用同值写入 localStorage，或访问 /?token=xxx 自动记住）。
        "access_token": "",
    },
    "billing": {
        "price_per_kwh": 0.6,
    },
    # 插口标签只存在本面板（设备固件不支持给单个插口命名）。
    # 整机设备名走 set_plug_name 真写进插座，官方 App 也能看到。
    "sockets": ["办公电脑主机", "电竞游戏主机", "鱼缸喂食器", "桌面显示器"],
    "limits": {
        "rated_watts": 2500,
    },
    "ui": {
        "default_range": "24h",
    },
}


def _deep_merge(base, override):
    """把 override 合并进 base 的副本，只合并 dict，不合并 list。"""
    out = copy.deepcopy(base)
    if not isinstance(override, dict):
        return out
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    # ---------- 读写 ----------

    def load(self):
        with self._lock:
            if not os.path.exists(self.path):
                self.data = copy.deepcopy(DEFAULT_CONFIG)
                self.save()
                return self.data
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if not isinstance(raw, dict):
                    raise ValueError("配置根节点必须是对象")
                self.data = _deep_merge(DEFAULT_CONFIG, raw)
            except Exception as exc:  # 配置坏了也不能让服务起不来
                backup = self.path + ".broken"
                try:
                    os.replace(self.path, backup)
                except OSError:
                    pass
                self.data = copy.deepcopy(DEFAULT_CONFIG)
                self.data["_load_error"] = "%s（已备份为 %s）" % (exc, os.path.basename(backup))
                self.save()
            return self.data

    def save(self):
        """原子落盘。"""
        with self._lock:
            directory = os.path.dirname(self.path)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            payload = {k: v for k, v in self.data.items() if not k.startswith("_")}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)

    # ---------- 访问 ----------

    def get(self, dotted, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted, value, persist=True):
        parts = dotted.split(".")
        with self._lock:
            node = self.data
            for part in parts[:-1]:
                if part not in node or not isinstance(node[part], dict):
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = value
            if persist:
                self.save()
        return value

    # ---------- 便捷属性 ----------

    @property
    def host(self):
        return str(self.get("device.host", "192.168.0.15")).strip()

    @property
    def port(self):
        return int(self.get("device.port", 6767))

    @property
    def timeout(self):
        return float(self.get("device.timeout", 3.0))

    @property
    def interval(self):
        value = self.get("sampler.interval_seconds", 2)
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 2
        return max(1, min(300, value))

    @property
    def price(self):
        try:
            value = float(self.get("billing.price_per_kwh", 0.6))
        except (TypeError, ValueError):
            value = 0.6
        return max(0.0, value)

    @property
    def socket_labels(self):
        labels = self.get("sockets", [])
        if not isinstance(labels, list):
            labels = []
        out = []
        for i in range(4):
            raw = labels[i] if i < len(labels) else ""
            out.append(str(raw)[:32] if raw else "插口 %d" % (i + 1))
        return out

    @property
    def db_path(self):
        path = str(self.get("storage.db_path", "data/p1pro.db"))
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(self.path), path)
        return path
