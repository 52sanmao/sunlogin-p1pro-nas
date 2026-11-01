/* ============================================================
   P1Chart — 自研 Canvas 时序图（约 10 KB）
   不用 Chart.js：一是 NAS 内网连不上 CDN，二是这里要的语义它做不好——
   缺失数据必须**断开**而不是连成直线，缺口要能看见，峰值要单独标出来。
   ============================================================ */

(function (global) {
  'use strict';

  var TEXT = '#64748b';
  var GRID = 'rgba(148, 163, 184, 0.10)';
  var GRID_STRONG = 'rgba(148, 163, 184, 0.18)';
  var ORANGE = '#f97316';
  var PEAK = 'rgba(148, 163, 184, 0.55)';
  var GAP_FILL = 'rgba(120, 53, 82, 0.07)';
  var GAP_LINE = 'rgba(190, 90, 130, 0.11)';

  function fmtNum(v) {
    if (v === null || v === undefined) return '--';
    if (v === 0) return '0';
    if (v >= 1000) return Math.round(v).toString();
    if (v >= 100) return v.toFixed(0);
    if (v >= 10) return v % 1 === 0 ? v.toFixed(0) : v.toFixed(1);
    if (v >= 1) return v.toFixed(1);
    return v.toFixed(2);
  }

  function niceMax(max) {
    if (!isFinite(max) || max <= 0) return 10;
    var exp = Math.floor(Math.log(max) / Math.LN10);
    var base = Math.pow(10, exp);
    var n = max / base;
    var step;
    if (n <= 1) step = 1;
    else if (n <= 2) step = 2;
    else if (n <= 2.5) step = 2.5;
    else if (n <= 5) step = 5;
    else step = 10;
    return step * base;
  }

  function pad2(n) { return n < 10 ? '0' + n : '' + n; }

  function timeLabel(ts, range) {
    var d = new Date(ts * 1000);
    if (range === '1y') return (d.getFullYear() % 100) + '/' + pad2(d.getMonth() + 1);
    if (range === '7d' || range === '30d') return pad2(d.getMonth() + 1) + '/' + pad2(d.getDate());
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function fullTime(ts, range) {
    var d = new Date(ts * 1000);
    var base = d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
    if (range === '1y') return d.getFullYear() + '年' + (d.getMonth() + 1) + '月';
    if (range === '7d' || range === '30d') return base;
    return base + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function P1Chart(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.data = null;
    this.hoverIndex = -1;
    this.onHover = null;
    this._dpr = 1;
    this._w = 0;
    this._h = 0;
    this._pts = [];
    this._bind();
  }

  P1Chart.prototype._bind = function () {
    var self = this;
    var move = function (ev) {
      var rect = self.canvas.getBoundingClientRect();
      var x = (ev.touches ? ev.touches[0].clientX : ev.clientX) - rect.left;
      self._setHover(x);
    };
    this.canvas.addEventListener('mousemove', move);
    this.canvas.addEventListener('touchstart', move, { passive: true });
    this.canvas.addEventListener('touchmove', move, { passive: true });
    this.canvas.addEventListener('mouseleave', function () {
      if (self.hoverIndex !== -1) {
        self.hoverIndex = -1;
        self.render();
        if (self.onHover) self.onHover(null);
      }
    });
    this.canvas.addEventListener('touchend', function () {
      self.hoverIndex = -1;
      self.render();
      if (self.onHover) self.onHover(null);
    });
  };

  P1Chart.prototype.setData = function (payload) {
    this.data = payload;
    this.hoverIndex = -1;
    this.render();
  };

  P1Chart.prototype._resize = function () {
    var rect = this.canvas.getBoundingClientRect();
    var dpr = global.devicePixelRatio || 1;
    var w = Math.max(1, Math.round(rect.width));
    var h = Math.max(1, Math.round(rect.height));
    if (this._w !== w || this._h !== h || this._dpr !== dpr) {
      this._w = w;
      this._h = h;
      this._dpr = dpr;
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
    }
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };

  P1Chart.prototype._layout = function () {
    var pad = { l: 46, r: 12, t: 16, b: 26 };
    if (this._w < 420) pad.l = 38;
    this.pad = pad;
    this.plotW = Math.max(10, this._w - pad.l - pad.r);
    this.plotH = Math.max(10, this._h - pad.t - pad.b);
  };

  P1Chart.prototype._setHover = function (px) {
    if (!this._pts.length) return;
    var pad = this.pad;
    var idx = -1;
    var best = Infinity;
    for (var i = 0; i < this._pts.length; i++) {
      var d = Math.abs(this._pts[i].x - px);
      if (d < best) { best = d; idx = i; }
    }
    if (best > 40) idx = -1;
    if (idx !== this.hoverIndex) {
      this.hoverIndex = idx;
      this.render();
      if (this.onHover) {
        this.onHover(idx === -1 ? null : {
          index: idx,
          point: this._pts[idx].raw,
          x: this._pts[idx].x,
          y: this._pts[idx].y == null ? this.pad.t : this._pts[idx].y,
          unit: this._unit
        });
      }
    }
  };

  P1Chart.prototype.render = function () {
    this._resize();
    this._layout();
    var ctx = this.ctx;
    var w = this._w, h = this._h, pad = this.pad;
    ctx.clearRect(0, 0, w, h);

    var data = this.data;
    if (!data || !data.points || !data.points.length) {
      this._pts = [];
      return;
    }

    var start = data.start, end = data.end;
    var span = Math.max(1, end - start);
    var points = data.points;

    var maxVal = 0;
    for (var i = 0; i < points.length; i++) {
      var v = points[i].pmax != null ? points[i].pmax : points[i].p;
      if (v != null && v > maxVal) maxVal = v;
    }
    var yMax = niceMax(maxVal * 1.08 || 10);
    this._unit = yMax >= 1000 ? 'kW' : 'W';
    this._yScale = yMax >= 1000 ? 0.001 : 1;

    var self = this;
    var xOf = function (t) { return pad.l + ((t - start) / span) * self.plotW; };
    var yOf = function (v) { return pad.t + self.plotH - (v / yMax) * self.plotH; };

    // ---- 网格 ----
    ctx.strokeStyle = GRID;
    ctx.lineWidth = 1;
    ctx.font = '11px -apple-system, "Segoe UI", "PingFang SC", sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = TEXT;
    var ticks = 4;
    for (var k = 0; k <= ticks; k++) {
      var val = (yMax / ticks) * k;
      var yy = yOf(val);
      ctx.beginPath();
      ctx.moveTo(pad.l, Math.round(yy) + 0.5);
      ctx.lineTo(pad.l + this.plotW, Math.round(yy) + 0.5);
      ctx.strokeStyle = k === 0 ? GRID_STRONG : GRID;
      ctx.stroke();
      var label = this._unit === 'kW' ? (val / 1000).toFixed(val >= 2000 ? 0 : 1) : fmtNum(val);
      ctx.fillText(label, pad.l - 7, yy);
    }

    this._pts = points.map(function (p) {
      return {
        raw: p,
        x: xOf(p.t),
        y: p.p == null ? null : yOf(p.p),
        ymax: p.pmax == null ? null : yOf(p.pmax)
      };
    });
    var pts = this._pts;
    var step = pts.length > 1 ? pts[1].x - pts[0].x : this.plotW;

    // ---- 缺失时段底纹 ----
    var i2 = 0;
    while (i2 < pts.length) {
      if (pts[i2].raw.p == null) {
        var j = i2;
        while (j < pts.length && pts[j].raw.p == null) j++;
        var gx0 = pts[i2].x - step / 2;
        var gx1 = pts[Math.min(j, pts.length - 1)].x + step / 2;
        gx0 = Math.max(pad.l, gx0);
        gx1 = Math.min(pad.l + this.plotW, gx1);
        if (gx1 > gx0) {
          ctx.fillStyle = GAP_FILL;
          ctx.fillRect(gx0, pad.t, gx1 - gx0, this.plotH);
          ctx.save();
          ctx.beginPath();
          ctx.rect(gx0, pad.t, gx1 - gx0, this.plotH);
          ctx.clip();
          ctx.strokeStyle = GAP_LINE;
          ctx.lineWidth = 1;
          ctx.beginPath();
          var slant = 9;
          for (var sx = gx0 - this.plotH; sx < gx1 + this.plotH; sx += slant * 3) {
            ctx.moveTo(sx, pad.t);
            ctx.lineTo(sx + this.plotH, pad.t + this.plotH);
          }
          ctx.stroke();
          ctx.restore();
        }
        i2 = j;
      } else {
        i2++;
      }
    }

    // ---- 分段（连续非空） ----
    var segments = [];
    var cur = [];
    for (var s = 0; s < pts.length; s++) {
      if (pts[s].y == null) {
        if (cur.length) { segments.push(cur); cur = []; }
      } else {
        cur.push(pts[s]);
      }
    }
    if (cur.length) segments.push(cur);

    // ---- 峰值虚线 ----
    ctx.save();
    ctx.strokeStyle = PEAK;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    for (var sg = 0; sg < segments.length; sg++) {
      ctx.beginPath();
      var seg = segments[sg];
      var started = false;
      for (var q = 0; q < seg.length; q++) {
        if (seg[q].ymax == null) { started = false; continue; }
        if (!started) { ctx.moveTo(seg[q].x, seg[q].ymax); started = true; }
        else ctx.lineTo(seg[q].x, seg[q].ymax);
      }
      ctx.stroke();
    }
    ctx.restore();

    // ---- 面积 + 主线 ----
    var baseY = pad.t + this.plotH;
    var grad = ctx.createLinearGradient(0, pad.t, 0, baseY);
    grad.addColorStop(0, 'rgba(249, 115, 22, 0.26)');
    grad.addColorStop(1, 'rgba(249, 115, 22, 0.01)');

    for (var g2 = 0; g2 < segments.length; g2++) {
      var sg2 = segments[g2];
      if (!sg2.length) continue;

      ctx.beginPath();
      this._trace(ctx, sg2);
      ctx.lineTo(sg2[sg2.length - 1].x, baseY);
      ctx.lineTo(sg2[0].x, baseY);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      ctx.beginPath();
      this._trace(ctx, sg2);
      ctx.strokeStyle = ORANGE;
      ctx.lineWidth = 2;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.stroke();

      // 单点也画出来
      if (sg2.length === 1) {
        ctx.beginPath();
        ctx.arc(sg2[0].x, sg2[0].y, 2.5, 0, Math.PI * 2);
        ctx.fillStyle = ORANGE;
        ctx.fill();
      }
    }

    // ---- 峰值标注 ----
    var peakIdx = -1, peakVal = -1;
    for (var p3 = 0; p3 < pts.length; p3++) {
      if (pts[p3].raw.p != null && pts[p3].raw.p > peakVal) {
        peakVal = pts[p3].raw.p; peakIdx = p3;
      }
    }
    if (peakIdx >= 0 && pts.length > 2 && peakVal > yMax * 0.12) {
      var pp = pts[peakIdx];
      ctx.beginPath();
      ctx.arc(pp.x, pp.y, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = '#0b0f19';
      ctx.fill();
      ctx.strokeStyle = ORANGE;
      ctx.lineWidth = 2;
      ctx.stroke();

      var txt = this._unit === 'kW' ? (peakVal / 1000).toFixed(2) + ' kW' : fmtNum(peakVal) + ' W';
      ctx.font = '10px -apple-system, "Segoe UI", "PingFang SC", sans-serif';
      var tw = ctx.measureText(txt).width;
      var tx = Math.min(pad.l + this.plotW - tw - 4, Math.max(pad.l + 2, pp.x - tw / 2));
      var ty = Math.max(pad.t + 9, pp.y - 8);
      ctx.fillStyle = 'rgba(249, 115, 22, 0.9)';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'bottom';
      ctx.fillText(txt, tx, ty);
    }

    // ---- X 轴标签 ----
    ctx.fillStyle = TEXT;
    ctx.font = '11px -apple-system, "Segoe UI", "PingFang SC", sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    var labelCount = Math.max(2, Math.min(7, Math.floor(this.plotW / 78)));
    for (var L = 0; L <= labelCount; L++) {
      var t = start + (span / labelCount) * L;
      var lx = pad.l + (this.plotW / labelCount) * L;
      ctx.textAlign = L === 0 ? 'left' : L === labelCount ? 'right' : 'center';
      ctx.fillText(timeLabel(t, data.range), lx, pad.t + this.plotH + 7);
    }

    // ---- 悬停 ----
    if (this.hoverIndex >= 0 && this.hoverIndex < pts.length) {
      var hp = pts[this.hoverIndex];
      ctx.save();
      ctx.strokeStyle = 'rgba(226, 232, 240, 0.34)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(hp.x) + 0.5, pad.t);
      ctx.lineTo(Math.round(hp.x) + 0.5, baseY);
      ctx.stroke();
      if (hp.y != null) {
        ctx.beginPath();
        ctx.arc(hp.x, hp.y, 4.5, 0, Math.PI * 2);
        ctx.fillStyle = '#0b0f19';
        ctx.fill();
        ctx.strokeStyle = '#fdba74';
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      ctx.restore();
    }
  };

  P1Chart.prototype._trace = function (ctx, seg) {
    if (!seg.length) return;
    ctx.moveTo(seg[0].x, seg[0].y);
    if (seg.length < 5) {
      for (var i = 1; i < seg.length; i++) ctx.lineTo(seg[i].x, seg[i].y);
      return;
    }
    // 水平中点贝塞尔：视觉上顺滑，又不会像 Catmull-Rom 那样过冲出真实量程
    for (var j = 0; j < seg.length - 1; j++) {
      var p0 = seg[j], p1 = seg[j + 1];
      var mx = (p0.x + p1.x) / 2;
      ctx.bezierCurveTo(mx, p0.y, mx, p1.y, p1.x, p1.y);
    }
  };

  P1Chart.prototype.destroy = function () {
    if (this._ro) this._ro.disconnect();
  };

  global.P1Chart = P1Chart;
  global.P1ChartUtil = {
    timeLabel: timeLabel,
    fullTime: fullTime,
    fmtNum: fmtNum
  };
})(window);
