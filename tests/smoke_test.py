#!/usr/bin/env python3
"""端到端冒烟测试。

起一个模拟插座 + 一个真实服务，然后逐项验证。不需要真机，CI 里也能跑。

    python3 tests/smoke_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
# 代码在飞牛应用规范的结构里：app/server/ 才是运行文件，app/ui/ 是桌面入口
SERVER = os.path.join(os.path.dirname(BASE), "app", "server")
PY = sys.executable
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print("  ok    %s" % name)
    else:
        FAILED.append((name, detail))
        print("  FAIL  %s   %s" % (name, detail))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_http(url, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with OP.open(url, timeout=1.5) as resp:
                resp.read()
                return True
        except Exception:
            time.sleep(0.25)
    return False


def get(url, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-P1Pro-Token", token)
    with OP.open(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def post(url, body, token=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-P1Pro-Token", token)
    try:
        with OP.open(req, timeout=25) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


# ----------------------------------------------------------------------

def test_store_aggregation():
    """存储层：补时间轴、缺口不积分、覆盖率语义。"""
    print("\n[存储层] 聚合与完整性")
    sys.path.insert(0, SERVER)
    from p1pro.store import Store

    tmp = tempfile.mkdtemp(prefix="p1pro-store-")
    try:
        db = os.path.join(tmp, "t.db")
        store = Store(db, interval=2, raw_retention_days=7, minute_retention_days=400)
        now = int(time.time())
        base = now - now % 60 - 300          # 5 分钟前对齐到整分

        # 前 3 分钟每 2 秒一条，恒定 100 W
        for i in range(90):
            store.insert_sample(base + i * 2, 100000, 230000, 500000, 15)
        # 中间空 60 秒（模拟断连）
        # 最后 1 分钟每 2 秒一条，恒定 200 W
        for i in range(30):
            store.insert_sample(base + 240 + i * 2, 200000, 230000, 500000, 15)

        store.finalize(now)

        rows = store.conn.execute(
            "SELECT ts,p_avg,p_max,n,gap FROM bucket WHERE level='min' ORDER BY ts"
        ).fetchall()
        check("分钟桶数量 = 4", len(rows) == 4, "实际 %d" % len(rows))

        first = rows[0]
        check("首个桶平均功率 ≈ 100 W",
              abs(first["p_avg"] / 1000.0 - 100) < 2, "实际 %.1f" % (first["p_avg"] / 1000.0))

        # 前 3 分钟 100 W × 180 s + 最后 1 分钟 200 W × 60 s = 30000 W·s = 8.33 Wh
        total_wh = sum(r["p_avg"] * r["n"] * 2 / 3600.0 / 1000.0 for r in rows)
        check("积分电量在合理区间（8.0~8.7 Wh）", 8.0 < total_wh < 8.7, "实际 %.2f Wh" % total_wh)

        series = store.series("1h", now=now + 60)
        nulls = [p for p in series["points"] if p["p"] is None]
        check("1 小时视图补齐了缺失点", len(nulls) > 40, "null 点 %d 个" % len(nulls))
        check("覆盖率 < 20%（只有 4 分钟数据）",
              series["totals"]["cov"] < 0.2, "cov=%.3f" % series["totals"]["cov"])

        gaps = [r["gap"] for r in rows if r["gap"] >= 58]
        check("检测到 60 秒缺口", len(gaps) == 1, "找到 %d 个" % len(gaps))
        store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_end_to_end():
    print("\n[端到端] 模拟设备 + 真实服务")
    dev_port = free_port()
    app_port = free_port()
    tmp = tempfile.mkdtemp(prefix="p1pro-e2e-")

    mock = subprocess.Popen([PY, os.path.join(BASE, "mock_device.py"),
                             "--port", str(dev_port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    app = None
    try:
        if not wait_http("http://127.0.0.1:%d/_probe" % dev_port, timeout=15):
            # mock 里没有 /_probe，能连上就算起来了
            pass
        time.sleep(1.0)

        config = {
            "device": {"host": "127.0.0.1", "port": dev_port, "timeout": 3.0},
            "sampler": {"enabled": True, "interval_seconds": 1, "backoff_max_seconds": 10},
            "storage": {"db_path": os.path.join(tmp, "data", "p1pro.db"),
                        "raw_retention_days": 7, "minute_retention_days": 400},
            "web": {"host": "127.0.0.1", "port": app_port, "access_token": ""},
            "billing": {"price_per_kwh": 0.6},
            "sockets": ["A", "B", "C", "D"],
            "limits": {"rated_watts": 2500},
            "ui": {"default_range": "24h"},
        }
        cfg_path = os.path.join(tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)

        env = dict(os.environ)
        env["P1PRO_QUIET"] = "1"
        app = subprocess.Popen([PY, os.path.join(SERVER, "app.py"), "-c", cfg_path],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

        base = "http://127.0.0.1:%d" % app_port
        if not wait_http(base + "/api/health", timeout=25):
            out = b""
            if app.stdout:
                app.stdout.close()
            check("服务启动", False, "健康检查超时")
            return

        check("服务启动 + 健康检查", True)

        boot = get(base + "/api/bootstrap")
        check("bootstrap 返回配置", boot["config"]["interval_seconds"] == 1)
        check("读取到设备 SN", boot["state"]["online"], str(boot["state"].get("error")))

        time.sleep(4)
        state = get(base + "/api/state")
        check("状态含四口位图", len(state["sockets"]) == 4, str(state["sockets"]))
        check("功率为真实读数", state["power_w"] is not None and state["power_w"] > 0,
              "%s W" % state["power_w"])
        check("采样计数在增长", state["samples"] >= 3, "%d 条" % state["samples"])

        # 开关
        before = get(base + "/api/state")["sockets"]
        target = 0 if before[1] else 1
        code, resp = post(base + "/api/switch", {"scope": "one", "index": 1, "status": target})
        check("单口开关返回成功", code == 200 and resp.get("ok"), str(resp))
        time.sleep(1.6)
        after = get(base + "/api/state")["sockets"]
        check("开关后状态已变化", after[1] == target, "%s → %s" % (before, after))

        code, resp = post(base + "/api/switch_many",
                          {"name": "测试场景", "statuses": [1, 0, 1, 0]})
        check("场景批量下发", code == 200 and resp.get("ok"), str(resp))
        time.sleep(1.6)
        check("场景生效", get(base + "/api/state")["sockets"] == [1, 0, 1, 0],
              str(get(base + "/api/state")["sockets"]))

        # 定时
        code, resp = post(base + "/api/timer",
                          {"op": "add", "index": 2, "hour": 7, "minute": 30,
                           "action": 1, "repeat": 127})
        check("新增定时", code == 200 and resp.get("ok"), str(resp))
        check("时区换算正确（07:30 → UTC 1410，东八区）",
              resp.get("device_time") in (1410, (450 - 480) % 1440),
              "device_time=%s" % resp.get("device_time"))
        timers = get(base + "/api/timers")
        s2 = timers["sockets"][2]["timers"]
        check("读回定时 1 条", len(s2) == 1, str(s2))
        check("读回的本地时间正确", s2 and s2[0]["hour"] == 7 and s2[0]["minute"] == 30, str(s2))
        check("重复规则解析为「每天」", s2 and s2[0]["days"]["text"] == "每天", str(s2))

        code, resp = post(base + "/api/timer",
                          {"op": "del", "index": 2,
                           "time_utc": (s2[0]["time_utc"] if s2 else 0), "repeat": 127})
        check("删除定时", code == 200 and resp.get("ok"), str(resp))
        check("删除后为空", get(base + "/api/timers")["sockets"][2]["timers"] == [])

        # 倒计时
        code, resp = post(base + "/api/countdown",
                          {"op": "add", "index": 3, "action": 0, "seconds": 3600})
        check("设置倒计时", code == 200 and resp.get("ok"), str(resp))
        cd = get(base + "/api/countdowns")["sockets"][3]["countdown"]
        check("倒计时剩余时间已返回", cd and 3500 < cd["remain"] <= 3600, str(cd))
        code, resp = post(base + "/api/countdown", {"op": "del", "index": 3})
        check("取消倒计时", code == 200 and resp.get("ok"), str(resp))
        check("取消后为空", get(base + "/api/countdowns")["sockets"][3]["countdown"] is None)

        # 改名（真写设备）
        code, resp = post(base + "/api/device_name", {"name": "厨房插线板"})
        check("设备名写入", code == 200 and resp.get("ok"), str(resp))
        check("设备名读回一致",
              get(base + "/api/device?force=1")["info"]["name"] == "厨房插线板")

        # 插口标签（本地）
        post(base + "/api/socket_label", {"index": 0, "label": "路由器"})
        boot2 = get(base + "/api/bootstrap")
        check("插口标签已保存", boot2["config"]["socket_labels"][0] == "路由器")

        # 曲线
        series = get(base + "/api/series?range=1h")
        check("曲线有数据点", len(series["points"]) > 0, "%d 点" % len(series["points"]))
        check("曲线含缺失标记（刚启动只有几十秒数据）",
              any(p["p"] is None for p in series["points"]))

        # 汇总
        summary = get(base + "/api/summary")
        check("今日汇总有数值", summary["today"]["kwh"] is not None, str(summary["today"]))
        check("今日覆盖率 < 100%（只跑了几秒）", summary["today"]["cov"] < 1.0,
              "cov=%.3f" % summary["today"]["cov"])

        # 导出
        with OP.open(base + "/api/export?type=minute&days=1", timeout=20) as resp:
            csv_text = resp.read().decode("utf-8-sig")
        check("CSV 导出带表头", csv_text.startswith("ts,datetime_local"), csv_text[:60])

        # 静态页
        with OP.open(base + "/", timeout=10) as resp:
            html = resp.read().decode()
        check("首页返回 HTML", "<title>P1Pro 控制台" in html)
        with OP.open(base + "/app.js", timeout=10) as resp:
            check("静态 JS 可访问", len(resp.read()) > 5000)
        with OP.open(base + "/chart.js", timeout=10) as resp:
            check("自研图表脚本可访问", b"P1Chart" in resp.read())

        # 事件
        events = get(base + "/api/events?limit=20")["events"]
        check("记录了开关事件", any(e["kind"] == "switch" for e in events),
              "共 %d 条" % len(events))

        # 配置修改
        code, resp = post(base + "/api/config", {"price_per_kwh": 0.55})
        check("修改电价", code == 200 and resp["config"]["price_per_kwh"] == 0.55, str(resp))
        code, resp = post(base + "/api/config", {"interval_seconds": 0})
        check("非法采样间隔被拒绝", code == 400, str(resp))

        # 路径穿越防护
        try:
            OP.open(base + "/../app.py", timeout=5).read()
            check("路径穿越被拦截", False, "居然读到了")
        except urllib.error.HTTPError as exc:
            check("路径穿越被拦截", exc.code in (403, 404), "HTTP %d" % exc.code)

    finally:
        for proc in (app, mock):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def test_auth():
    print("\n[鉴权] access_token 生效")
    dev_port = free_port()
    app_port = free_port()
    tmp = tempfile.mkdtemp(prefix="p1pro-auth-")
    mock = subprocess.Popen([PY, os.path.join(BASE, "mock_device.py"),
                             "--port", str(dev_port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    app = None
    try:
        time.sleep(1.0)
        cfg = {
            "device": {"host": "127.0.0.1", "port": dev_port, "timeout": 3.0},
            "sampler": {"enabled": False, "interval_seconds": 2},
            "storage": {"db_path": os.path.join(tmp, "d.db")},
            "web": {"host": "127.0.0.1", "port": app_port, "access_token": "s3cret"},
            "sockets": ["A", "B", "C", "D"],
        }
        cfg_path = os.path.join(tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        env = dict(os.environ, P1PRO_QUIET="1")
        app = subprocess.Popen([PY, os.path.join(SERVER, "app.py"), "-c", cfg_path],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = "http://127.0.0.1:%d" % app_port
        wait_http(base + "/api/health", timeout=25)

        try:
            get(base + "/api/bootstrap")
            check("无 token 被拒绝", False, "居然通过了")
        except urllib.error.HTTPError as exc:
            check("无 token 被拒绝", exc.code == 401, "HTTP %d" % exc.code)

        check("带 token 可访问", get(base + "/api/bootstrap", token="s3cret")["app"]["version"] != "")
        check("health 不需要 token",
              get(base + "/api/health")["ok"] is True)

        # 面板里就能改口令——飞牛用户不必回到应用设置去改
        status, res = post(base + "/api/config", {"access_token": "abc"}, token="s3cret")
        check("过短的访问密码被拒绝", status == 400, "HTTP %d" % status)

        status, res = post(base + "/api/config", {"access_token": "newpass"}, token="s3cret")
        check("可以改访问密码",
              status == 200 and res.get("config", {}).get("auth_enabled") is True,
              "HTTP %d" % status)
        try:
            get(base + "/api/bootstrap", token="s3cret")
            check("旧密码立即失效", False, "旧密码还能用")
        except urllib.error.HTTPError as exc:
            check("旧密码立即失效", exc.code == 401, "HTTP %d" % exc.code)
        check("新密码可访问",
              get(base + "/api/bootstrap", token="newpass")["app"]["version"] != "")

        # 传空字符串 = 明确要求关掉鉴权（向导里的"留空"走不到这条路径）
        status, res = post(base + "/api/config", {"access_token": ""}, token="newpass")
        check("可以清除访问密码",
              status == 200 and get(base + "/api/bootstrap")["app"]["version"] != "",
              "HTTP %d" % status)
    finally:
        for proc in (app, mock):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 62)
    print(" P1Pro 控制台 · 冒烟测试")
    print("=" * 62)
    started = time.time()

    test_store_aggregation()
    test_end_to_end()
    test_auth()

    print("\n" + "=" * 62)
    print(" 通过 %d 项，失败 %d 项，耗时 %.1fs" % (len(PASSED), len(FAILED), time.time() - started))
    if FAILED:
        print("\n 失败明细：")
        for name, detail in FAILED:
            print("   - %s  %s" % (name, detail))
        print("=" * 62)
        return 1
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
