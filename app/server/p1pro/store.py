"""时序存储：样本 → 分钟 → 小时 → 天 四层金字塔。

为什么这么设计——设备**一个字节的历史都不存**（``get_plug_electric`` 只回三个瞬时值，
所有历史类方法名实测全返回 ``result:1``）。所以曲线只能靠本地累计，
存储层必须同时满足三件事：秒级细节、长期体积、以及**能说清"这段数据缺了没有"**。

最后一件事是旧版最大的语义错误：断电或离线期间积分不累加，界面显示 ``0.000 kWh``，
用户读到的是"没耗电"，而事实是"没采到"。这里的做法是每个桶都记 ``n``（样本数）
和 ``gap``（桶内最大采样间隔），查询时算出覆盖率：

* ``cov >= 0.8``  → 正常，返回数值
* ``0.2 <= cov``  → 返回数值但标 ``partial``，前端画虚线段
* ``cov < 0.2``   → 返回 ``null``，前端断线，绝不显示成 0
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS sample (
    ts  INTEGER PRIMARY KEY,   -- epoch 秒（本机时钟）
    p   INTEGER,               -- 有功功率 mW
    u   INTEGER,               -- 电压 mV
    i   INTEGER,               -- 视在电流 µA
    st  INTEGER                -- 四口状态位图
);

CREATE TABLE IF NOT EXISTS bucket (
    level     TEXT    NOT NULL,   -- 'min' | 'hour' | 'day'
    ts        INTEGER NOT NULL,   -- 桶起点 epoch 秒
    p_avg     REAL,               -- 时间加权平均功率 mW
    p_max     INTEGER,            -- 峰值 mW
    p_min     INTEGER,            -- 谷值 mW
    energy_wh REAL,               -- 该桶积分电量 Wh
    n         INTEGER,            -- 实际样本数
    gap       INTEGER,            -- 桶内最大采样间隔（秒）
    PRIMARY KEY (level, ts)
);

CREATE TABLE IF NOT EXISTS event (
    ts     INTEGER NOT NULL,
    kind   TEXT    NOT NULL,      -- switch / timer / countdown / link / system
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event(ts);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

RANGES = {
    # name: (level, seconds, 目标点数提示)
    "1h": ("min", 3600),
    "6h": ("min", 6 * 3600),
    "24h": ("min", 24 * 3600),
    "7d": ("hour", 7 * 86400),
    "30d": ("hour", 30 * 86400),
    "1y": ("day", 365 * 86400),
}

LEVEL_SECONDS = {"min": 60, "hour": 3600, "day": 86400}

TARGET_POINTS = 360


class Store:
    def __init__(self, path, interval=2, raw_retention_days=7, minute_retention_days=400):
        self.path = os.path.abspath(path)
        self.interval = max(1, int(interval))
        self.raw_retention_days = max(1, int(raw_retention_days))
        self.minute_retention_days = max(2, int(minute_retention_days))
        self._lock = threading.RLock()
        self._last_prune = 0.0
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15.0)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ------------------------------------------------------------------
    # 元数据
    # ------------------------------------------------------------------

    def _meta_get(self, key, default=None):
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def _meta_set(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (key, str(value)))

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def insert_sample(self, ts, p_mw, u_mv, i_ua, state):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO sample(ts,p,u,i,st) VALUES(?,?,?,?,?)",
                (int(ts), int(p_mw), int(u_mv), int(i_ua), int(state)),
            )
            self.conn.commit()

    def add_event(self, kind, detail, ts=None):
        with self._lock:
            self.conn.execute(
                "INSERT INTO event(ts,kind,detail) VALUES(?,?,?)",
                (int(ts or time.time()), str(kind), str(detail)[:300]),
            )
            self.conn.commit()

    def events(self, limit=60):
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts,kind,detail FROM event ORDER BY ts DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------------

    def finalize(self, now=None):
        """把所有已闭合的桶补算完。可反复调用，幂等。"""
        now = int(now or time.time())
        with self._lock:
            self._finalize_minutes(now)
            self._finalize_hours(now)
            self._finalize_days(now)
            if now - self._last_prune > 3600:
                self._prune(now)
                self._last_prune = now

    def _finalize_minutes(self, now):
        cur = now - now % 60
        cursor = int(self._meta_get("min_cursor", 0) or 0)
        if not cursor:
            row = self.conn.execute("SELECT MIN(ts) AS m FROM sample").fetchone()
            cursor = (int(row["m"]) // 60 * 60) if row and row["m"] else cur
        if cursor >= cur:
            return
        rows = self.conn.execute(
            "SELECT ts,p FROM sample WHERE ts>=? AND ts<? ORDER BY ts", (cursor, cur)
        ).fetchall()
        if not rows:
            self._meta_set("min_cursor", cur)
            self.conn.commit()
            return

        prev = self.conn.execute(
            "SELECT ts FROM sample WHERE ts<? ORDER BY ts DESC LIMIT 1", (cursor,)
        ).fetchone()
        prev_ts = int(prev["ts"]) if prev else None
        max_gap = max(self.interval * 3, 10)

        groups = {}
        for row in rows:
            ts = int(row["ts"])
            p = int(row["p"])
            raw_dt = 0
            if prev_ts is None:
                dt = self.interval
            else:
                raw_dt = ts - prev_ts
                dt = raw_dt
                if dt <= 0:
                    dt = self.interval
                elif dt > max_gap:
                    dt = 0          # 缺口不积分，避免把离线期间"补"成耗电
            prev_ts = ts
            key = ts // 60 * 60
            g = groups.get(key)
            if g is None:
                g = {"w": 0.0, "sum": 0.0, "pmax": p, "pmin": p, "e": 0.0, "n": 0, "gap": 0}
                groups[key] = g
            g["n"] += 1
            if p > g["pmax"]:
                g["pmax"] = p
            if p < g["pmin"]:
                g["pmin"] = p
            if dt > 0:
                g["w"] += dt
                g["sum"] += p * dt
                g["e"] += p * dt / 3600.0 / 1000.0
            # 缺口长度要单独记：dt 已经被置 0 用于跳过积分，
            # 但 gap 字段的用途恰恰是"报告这里断了多久"，不能被一起抹掉。
            if raw_dt > g["gap"]:
                g["gap"] = raw_dt

        payload = []
        for key, g in groups.items():
            avg = (g["sum"] / g["w"]) if g["w"] else float(g["pmax"])
            payload.append(("min", key, avg, g["pmax"], g["pmin"], g["e"], g["n"], g["gap"]))
        self.conn.executemany(
            "INSERT OR REPLACE INTO bucket(level,ts,p_avg,p_max,p_min,energy_wh,n,gap) "
            "VALUES(?,?,?,?,?,?,?,?)", payload
        )
        self._meta_set("min_cursor", cur)
        self.conn.commit()

    def _finalize_hours(self, now):
        cur = now - now % 3600
        cursor = int(self._meta_get("hour_cursor", 0) or 0)
        if not cursor:
            row = self.conn.execute("SELECT MIN(ts) AS m FROM bucket WHERE level='min'").fetchone()
            cursor = (int(row["m"]) // 3600 * 3600) if row and row["m"] else cur
        if cursor >= cur:
            return
        rows = self.conn.execute(
            "SELECT ts,p_avg,p_max,p_min,energy_wh,n,gap FROM bucket "
            "WHERE level='min' AND ts>=? AND ts<? ORDER BY ts", (cursor, cur)
        ).fetchall()
        if not rows:
            self._meta_set("hour_cursor", cur)
            self.conn.commit()
            return
        groups = {}
        for row in rows:
            key = int(row["ts"]) // 3600 * 3600
            g = groups.get(key)
            n = int(row["n"] or 0)
            if g is None:
                g = {"wsum": 0.0, "wn": 0, "pmax": int(row["p_max"] or 0),
                     "pmin": int(row["p_min"] or 0), "e": 0.0, "n": 0, "gap": 0}
                groups[key] = g
            g["wsum"] += float(row["p_avg"] or 0) * n
            g["wn"] += n
            g["pmax"] = max(g["pmax"], int(row["p_max"] or 0))
            pmin = int(row["p_min"] or 0)
            if g["pmin"] == 0 or pmin < g["pmin"]:
                g["pmin"] = pmin
            g["e"] += float(row["energy_wh"] or 0)
            g["n"] += n
            g["gap"] = max(g["gap"], int(row["gap"] or 0))
        payload = [
            ("hour", key, (g["wsum"] / g["wn"]) if g["wn"] else 0.0,
             g["pmax"], g["pmin"], g["e"], g["n"], g["gap"])
            for key, g in groups.items()
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO bucket(level,ts,p_avg,p_max,p_min,energy_wh,n,gap) "
            "VALUES(?,?,?,?,?,?,?,?)", payload
        )
        self._meta_set("hour_cursor", cur)
        self.conn.commit()

    def _finalize_days(self, now):
        # 用本地时区的自然日边界
        lt = time.localtime(now)
        day_start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        cursor = int(self._meta_get("day_cursor", 0) or 0)
        if not cursor:
            row = self.conn.execute("SELECT MIN(ts) AS m FROM bucket WHERE level='hour'").fetchone()
            if row and row["m"]:
                t = time.localtime(int(row["m"]))
                cursor = int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))
            else:
                cursor = day_start
        if cursor >= day_start:
            return
        rows = self.conn.execute(
            "SELECT ts,p_avg,p_max,p_min,energy_wh,n,gap FROM bucket "
            "WHERE level='hour' AND ts>=? AND ts<? ORDER BY ts", (cursor, day_start)
        ).fetchall()
        if not rows:
            self._meta_set("day_cursor", day_start)
            self.conn.commit()
            return
        groups = {}
        for row in rows:
            t = time.localtime(int(row["ts"]))
            key = int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))
            n = int(row["n"] or 0)
            g = groups.get(key)
            if g is None:
                g = {"wsum": 0.0, "wn": 0, "pmax": int(row["p_max"] or 0),
                     "pmin": int(row["p_min"] or 0), "e": 0.0, "n": 0, "gap": 0}
                groups[key] = g
            g["wsum"] += float(row["p_avg"] or 0) * n
            g["wn"] += n
            g["pmax"] = max(g["pmax"], int(row["p_max"] or 0))
            pmin = int(row["p_min"] or 0)
            if g["pmin"] == 0 or pmin < g["pmin"]:
                g["pmin"] = pmin
            g["e"] += float(row["energy_wh"] or 0)
            g["n"] += n
            g["gap"] = max(g["gap"], int(row["gap"] or 0))
        payload = [
            ("day", key, (g["wsum"] / g["wn"]) if g["wn"] else 0.0,
             g["pmax"], g["pmin"], g["e"], g["n"], g["gap"])
            for key, g in groups.items()
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO bucket(level,ts,p_avg,p_max,p_min,energy_wh,n,gap) "
            "VALUES(?,?,?,?,?,?,?,?)", payload
        )
        self._meta_set("day_cursor", day_start)
        self.conn.commit()

    def _prune(self, now):
        raw_cut = now - self.raw_retention_days * 86400
        self.conn.execute("DELETE FROM sample WHERE ts<?", (raw_cut,))
        min_cut = now - self.minute_retention_days * 86400
        self.conn.execute("DELETE FROM bucket WHERE level='min' AND ts<?", (min_cut,))
        self.conn.execute("DELETE FROM event WHERE ts<?", (now - 365 * 86400,))
        self.conn.commit()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _live_bucket(self, now, level):
        """把"还没闭合的当前桶"从原始样本临时算出来，避免曲线尾部慢 60 秒。"""
        span = LEVEL_SECONDS[level]
        start = now - now % span
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, SUM(p) AS s, MAX(p) AS mx, MIN(p) AS mn FROM sample WHERE ts>=?",
            (start,)
        ).fetchone()
        n = int(row["n"] or 0)
        if n == 0:
            return None
        avg = float(row["s"]) / n
        elapsed = max(1, now - start)
        return {
            "level": level, "ts": start, "p_avg": avg,
            "p_max": int(row["mx"] or 0), "p_min": int(row["mn"] or 0),
            "energy_wh": avg * n * self.interval / 3600.0 / 1000.0,
            "n": n, "gap": 0,
            # 只过去了 elapsed 秒，覆盖率要按已过去的时间算，
            # 否则刚开机的头几十秒会被判成"数据缺失"而断线。
            "span": elapsed,
        }

    def series(self, range_name, now=None):
        now = int(now or time.time())
        level, span = RANGES.get(range_name, RANGES["24h"])
        bucket_sec = LEVEL_SECONDS[level]

        if level == "day":
            # 日桶按**本地自然日**切分，不能按 UTC 0 点对齐，否则会错 8 小时
            lt = time.localtime(now - span)
            start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        else:
            start = now - span
            start -= start % bucket_sec

        with self._lock:
            rows = self.conn.execute(
                "SELECT ts,p_avg,p_max,p_min,energy_wh,n,gap FROM bucket "
                "WHERE level=? AND ts>=? ORDER BY ts", (level, start)
            ).fetchall()
            known = {}
            for r in rows:
                known[int(r["ts"])] = {
                    "ts": int(r["ts"]),
                    "p_avg": float(r["p_avg"] or 0),
                    "p_max": int(r["p_max"] or 0),
                    "p_min": int(r["p_min"] or 0),
                    "energy_wh": float(r["energy_wh"] or 0),
                    "n": int(r["n"] or 0),
                    "gap": int(r["gap"] or 0),
                    "span": bucket_sec,
                }
            live = self._live_bucket(now, level)
            if live:
                known[live["ts"]] = live

        # 补齐时间轴：没有数据的桶也返回一个 n=0 的点。
        # 少了这一步，图上的"缺一段"会画成"什么也没有"——看起来像数据完整，
        # 实际是这一整天压根没采到东西。
        points = []
        cursor = start
        guard = 0
        while cursor <= now and guard < 2000:
            guard += 1
            if cursor in known:
                points.append(known[cursor])
            else:
                points.append({
                    "ts": cursor, "p_avg": 0.0, "p_max": 0, "p_min": 0,
                    "energy_wh": 0.0, "n": 0, "gap": 0, "span": bucket_sec,
                })
            cursor += bucket_sec

        points = self._downsample(points, TARGET_POINTS)
        out = []
        sum_e = 0.0
        peak = 0.0
        total_n = 0
        for p in points:
            p_span = max(1, int(p.get("span") or bucket_sec))
            expected = max(1.0, p_span / float(self.interval))
            cov = min(1.0, p["n"] / expected)
            total_n += p["n"]
            if cov >= 0.2:
                sum_e += p["energy_wh"]
                peak = max(peak, p["p_max"])
            out.append({
                "t": p["ts"],
                "p": None if cov < 0.2 else round(p["p_avg"] / 1000.0, 3),
                "pmax": None if cov < 0.2 else round(p["p_max"] / 1000.0, 3),
                "e": None if cov < 0.2 else round(p["energy_wh"] / 1000.0, 6),
                "cov": round(cov, 3),
                "partial": 0.2 <= cov < 0.8,
            })

        # 整体覆盖率必须按**请求的时间范围**算，而不是按有数据的点跨度算——
        # 否则刚部署（只有两分钟数据）会算出 100%，把"这一整天几乎没数据"藏起来。
        exp_total = max(1.0, (now - start) / float(self.interval))
        totals = {
            "kwh": round(sum_e / 1000.0, 4),
            "peak_w": round(peak / 1000.0, 1),
            "cov": round(min(1.0, total_n / exp_total), 4),
            "points": len(out),
        }
        return {
            "range": range_name,
            "level": level,
            "start": start,
            "end": now,
            "interval_seconds": self.interval,
            "points": out,
            "totals": totals,
            "empty": all(p["p"] is None for p in out) if out else True,
        }

    def _downsample(self, points, target):
        if len(points) <= target:
            return points
        step = int(len(points) / float(target)) + 1
        merged = []
        for i in range(0, len(points), step):
            chunk = points[i:i + step]
            n = sum(c["n"] for c in chunk)
            if n:
                avg = sum(c["p_avg"] * c["n"] for c in chunk) / n
            else:
                avg = 0.0
            merged.append({
                "ts": chunk[0]["ts"],
                "p_avg": avg,
                "p_max": max(c["p_max"] for c in chunk),
                "p_min": min(c["p_min"] for c in chunk),
                "energy_wh": sum(c["energy_wh"] for c in chunk),
                "n": n,
                "gap": max(c["gap"] for c in chunk),
                # 合并后这一点的实际跨度变大，覆盖率要按放大后的跨度算，
                # 否则 n 累加而 expected 不变，cov 会永远顶到 1.0，遮蔽真实缺口。
                "span": sum(int(c.get("span") or 0) for c in chunk),
            })
        return merged

    def summary(self, now=None):
        now = int(now or time.time())
        lt = time.localtime(now)
        day_start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        month_start = int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
        year_start = int(time.mktime((lt.tm_year, 1, 1, 0, 0, 0, 0, 0, -1)))

        def _agg(start, end):
            with self._lock:
                row = self.conn.execute(
                    "SELECT SUM(energy_wh) AS e, MAX(p_max) AS mx, SUM(n) AS n "
                    "FROM bucket WHERE level='min' AND ts>=? AND ts<?", (start, end)
                ).fetchone()
                e_wh = float(row["e"] or 0.0)
                peak_mw = int(row["mx"] or 0)
                n = int(row["n"] or 0)
                # 当前这一分钟还没闭合，bucket 里没有它；
                # 补上实时部分，否则刚部署或刚过零点时会显示成"无数据"。
                now_min = now - now % 60
                if start <= now_min < end:
                    exists = self.conn.execute(
                        "SELECT 1 FROM bucket WHERE level='min' AND ts=?", (now_min,)
                    ).fetchone()
                    if not exists:
                        live = self._live_bucket(now, "min")
                        if live:
                            e_wh += float(live["energy_wh"])
                            n += int(live["n"])
                            peak_mw = max(peak_mw, int(live["p_max"]))
            expected = max(1.0, (end - start) / float(self.interval))
            cov = min(1.0, n / expected)
            return {
                "kwh": None if n == 0 else round(e_wh / 1000.0, 4),
                "peak_w": None if n == 0 else round(peak_mw / 1000.0, 1),
                "cov": round(cov, 3),
                "samples": n,
                "complete": cov >= 0.8,
            }

        today = _agg(day_start, now + 60)
        month = _agg(month_start, now + 60)
        week = _agg(now - 7 * 86400, now + 60)
        year = _agg(year_start, now + 60)
        return {"today": today, "month": month, "week": week, "year": year, "now": now}

    def recent_energy(self, seconds=3600, now=None):
        now = int(now or time.time())
        with self._lock:
            row = self.conn.execute(
                "SELECT SUM(energy_wh) AS e FROM bucket WHERE level='min' AND ts>=?",
                (now - seconds,)
            ).fetchone()
        return round(float(row["e"] or 0.0) / 1000.0, 4)

    def stats(self):
        with self._lock:
            samples = self.conn.execute("SELECT COUNT(*) AS c FROM sample").fetchone()["c"]
            buckets = self.conn.execute("SELECT COUNT(*) AS c FROM bucket").fetchone()["c"]
            events = self.conn.execute("SELECT COUNT(*) AS c FROM event").fetchone()["c"]
            first = self.conn.execute("SELECT MIN(ts) AS m FROM sample").fetchone()["m"]
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = 0
        # WAL 模式下新写入的数据还在 -wal 里，不算进去会显示成"只有 4 KB"
        for suffix in ("-wal", "-shm"):
            try:
                size += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return {
            "samples": int(samples),
            "buckets": int(buckets),
            "events": int(events),
            "first_sample_ts": int(first) if first else None,
            "db_bytes": size,
        }

    def close(self):
        with self._lock:
            try:
                self.conn.commit()
                self.conn.close()
            except Exception:
                pass
