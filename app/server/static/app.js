/* ============================================================
   P1Pro 控制台 · 前端逻辑
   数据全部来自本地 API，没有任何模拟值（原稿里的 Math.random、
   hiddenWeightW、写死的 history 数组全部删掉了）。
   ============================================================ */

(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  var TOKEN = (function () {
    var params = new URLSearchParams(location.search);
    var fromUrl = params.get('token');
    if (fromUrl) {
      try { localStorage.setItem('p1pro_token', fromUrl); } catch (e) {}
      params.delete('token');
      var qs = params.toString();
      history.replaceState({}, '', location.pathname + (qs ? '?' + qs : ''));
      return fromUrl;
    }
    try { return localStorage.getItem('p1pro_token') || ''; } catch (e) { return ''; }
  })();

  var S = {
    boot: null,
    config: null,
    state: null,
    summary: null,
    series: null,
    timers: null,
    countdowns: null,
    cdFetchedAt: 0,
    events: [],
    device: null,
    range: '24h',
    price: 0.6,
    rated: 2500,
    chart: null,
    busy: false,
    lastToast: { msg: '', at: 0 }
  };

  var SCENES = {
    work: { name: '远程办公', states: [1, 0, 1, 1] },
    gaming: { name: '游戏预热', states: [0, 1, 1, 1] },
    fishtank: { name: '鱼缸喂食', states: [0, 0, 1, 0] }
  };

  var REPEAT_LABELS = ['日', '一', '二', '三', '四', '五', '六'];

  // ------------------------------------------------------------------
  // 工具
  // ------------------------------------------------------------------

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function pad(n) { return n < 10 ? '0' + n : '' + n; }

  function num(v, d) {
    if (v === null || v === undefined || isNaN(v)) return '--';
    return Number(v).toFixed(d === undefined ? 1 : d);
  }

  function money(kwh) {
    if (kwh === null || kwh === undefined) return '--';
    return '¥' + (kwh * S.price).toFixed(2);
  }

  function fmtRemain(sec) {
    sec = Math.max(0, Math.floor(sec));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    if (h > 0) return h + ':' + pad(m) + ':' + pad(s);
    return pad(m) + ':' + pad(s);
  }

  function fmtAgo(ts) {
    var d = new Date(ts * 1000);
    return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function fmtBytes(n) {
    if (!n) return '0 B';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }

  function toast(msg, type) {
    if (!msg) return;
    var now = Date.now();
    if (S.lastToast.msg === msg && now - S.lastToast.at < 2500) return;
    S.lastToast = { msg: msg, at: now };
    var box = document.createElement('div');
    box.className = 'toast ' + (type || '');
    box.innerHTML = '<span class="t-dot"></span><span>' + esc(msg) + '</span>';
    $('toastWrap').appendChild(box);
    setTimeout(function () {
      box.classList.add('out');
      setTimeout(function () { box.remove(); }, 220);
    }, type === 'err' ? 4200 : 2600);
  }

  // ------------------------------------------------------------------
  // API
  // ------------------------------------------------------------------

  function api(path, options) {
    options = options || {};
    var headers = {};
    if (TOKEN) headers['X-P1Pro-Token'] = TOKEN;
    var init = { method: options.method || 'GET', headers: headers, cache: 'no-store' };
    if (options.body) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body);
    }
    return fetch(path, init).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          throw new Error(data.error || ('HTTP ' + res.status));
        }
        if (data && data.ok === false) {
          throw new Error(data.error || '操作失败');
        }
        return data;
      });
    });
  }

  // ------------------------------------------------------------------
  // 渲染：插线板
  // ------------------------------------------------------------------

  function socketTemplate(i) {
    return '' +
      '<div class="socket" data-socket="' + i + '">' +
        '<div class="socket-top">' +
          '<span class="socket-code">S' + (i + 1) + '</span>' +
          '<span class="socket-state">OFF</span>' +
        '</div>' +
        '<button class="plug-btn" data-toggle="' + i + '" type="button" title="点击开关 S' + (i + 1) + '">' +
          '<span class="plug-graphic">' +
            '<span class="plug-row"><i class="hole"></i><i class="hole"></i></span>' +
            '<span class="plug-col">' +
              '<i class="hole hole-ground"></i>' +
              '<span class="plug-row plug-tight"><i class="hole tilt-l"></i><i class="hole tilt-r"></i></span>' +
            '</span>' +
          '</span>' +
        '</button>' +
        '<div class="socket-foot">' +
          '<span class="socket-name" data-name="' + i + '">插口 ' + (i + 1) + '</span>' +
          '<button class="icon-btn" data-rename="' + i + '" type="button" title="修改名称">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
            '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>' +
          '</button>' +
          '<span class="led led-off" data-led="' + i + '"></span>' +
        '</div>' +
      '</div>';
  }

  function buildSockets() {
    var html = '';
    for (var i = 0; i < 4; i++) html += socketTemplate(i);
    $('sockets').innerHTML = html;
  }

  function updateSockets() {
    var st = S.state || {};
    var sockets = st.sockets || [0, 0, 0, 0];
    var labels = (S.config && S.config.socket_labels) || [];
    var anyOn = false;

    for (var i = 0; i < 4; i++) {
      var on = !!sockets[i];
      if (on) anyOn = true;
      var card = document.querySelector('[data-socket="' + i + '"]');
      if (!card) continue;
      card.classList.toggle('on', on);
      var stateEl = card.querySelector('.socket-state');
      if (stateEl) stateEl.textContent = on ? 'ON' : 'OFF';
      var nameEl = card.querySelector('[data-name="' + i + '"]');
      if (nameEl) nameEl.textContent = labels[i] || ('插口 ' + (i + 1));
      var led = card.querySelector('[data-led="' + i + '"]');
      if (led) {
        led.className = 'led ' + (!st.online ? 'led-off' : (on ? 'led-on' : 'led-off'));
      }
    }

    var master = $('masterLed');
    if (master) {
      master.className = 'led led-bar ' + (!st.online ? 'led-off' : (anyOn ? 'led-on' : 'led-off'));
    }
  }

  // ------------------------------------------------------------------
  // 渲染：指标
  // ------------------------------------------------------------------

  function updateMetrics() {
    var st = S.state || {};
    var online = !!st.online;
    var power = st.power_w;
    var sockets = st.sockets || [0, 0, 0, 0];
    var active = sockets.reduce(function (a, b) { return a + (b ? 1 : 0); }, 0);

    $('mPower').textContent = online ? num(power, power >= 100 ? 0 : 1) : '--';
    $('mCurrent').textContent = online ? num(st.current_a, 2) : '--';
    $('mVoltage').textContent = online ? num(st.voltage_v, 1) : '--';
    $('mActive').textContent = active + '/4';

    var pct = 0;
    if (online && power != null && S.rated > 0) pct = Math.min(100, (power / S.rated) * 100);
    $('powerBar').style.width = Math.max(power > 0 ? 2 : 0, pct) + '%';
    $('ratedHint').textContent = '≤' + S.rated + 'W';

    var badge = $('loadBadge');
    if (!online) {
      badge.textContent = '离线';
      badge.className = 'pill pill-red';
    } else if (power != null && power > S.rated) {
      badge.textContent = '超载';
      badge.className = 'pill pill-red';
    } else if (power != null && power > S.rated * 0.7) {
      badge.textContent = '高负载';
      badge.className = 'pill pill-amber';
    } else if (active === 0) {
      badge.textContent = '全关';
      badge.className = 'pill pill-dim';
    } else {
      badge.textContent = '正常';
      badge.className = 'pill pill-green';
    }

    $('mInterval').textContent = (S.config && S.config.interval_seconds) || '--';
    $('mLatency').textContent = online ? num(st.latency_ms, 0) : '--';
    $('mSamples').textContent = st.samples != null ? st.samples : '--';

    // 链路徽章
    var lb = $('linkBadge');
    var text = '连接中…';
    lb.className = 'link-badge';
    if (online) {
      lb.classList.add('online');
      text = '在线 · ' + num(st.latency_ms, 0) + 'ms';
      if (st.age_seconds != null && st.age_seconds > (S.config ? S.config.interval_seconds * 4 : 12)) {
        lb.className = 'link-badge warn';
        text = '采样停滞 ' + st.age_seconds + 's';
      }
    } else if (st.error) {
      lb.classList.add('offline');
      text = st.error.length > 22 ? st.error.slice(0, 22) + '…' : st.error;
    }
    $('linkText').textContent = text;
    $('linkBadge').title = online
      ? '本地直连 ' + ((S.config && S.config.host) || '') + ':' + ((S.config && S.config.port) || 6767)
      : (st.error || '未连接');
  }

  function updateSummary() {
    var sm = S.summary;
    if (!sm) return;
    var today = sm.today || {};
    var month = sm.month || {};

    $('mToday').textContent = num(today.kwh, today.kwh != null && today.kwh < 10 ? 3 : 2);
    $('mTodayCost').textContent = money(today.kwh);
    $('mMonth').textContent = num(month.kwh, month.kwh != null && month.kwh < 10 ? 3 : 1);
    $('mMonthCost').textContent = money(month.kwh);

    // 缺数据时明确说"缺口"，而不是显示成 0
    setCovHint($('todayCov'), today);
    setCovHint($('monthCov'), month);
  }

  function setCovHint(el, agg) {
    if (!el) return;
    if (!agg || agg.samples === 0) {
      el.textContent = '暂无数据';
      el.className = 'hint';
      return;
    }
    var cov = Math.round((agg.cov || 0) * 100);
    if (cov >= 90) {
      el.textContent = '完整';
      el.className = 'hint';
    } else if (cov >= 40) {
      el.textContent = '覆盖 ' + cov + '%';
      el.className = 'hint warn';
    } else {
      el.textContent = '缺口较大';
      el.className = 'hint bad';
    }
  }

  // ------------------------------------------------------------------
  // 渲染：图表
  // ------------------------------------------------------------------

  function renderChart() {
    var s = S.series;
    if (!s) return;
    $('chartEmpty').hidden = !s.empty;
    if (S.chart) S.chart.setData(s);
    updateRangeSummary();
  }

  function rangeLabel(range) {
    return { '1h': '近 1 小时', '6h': '近 6 小时', '24h': '近 24 小时', '7d': '近 7 天', '30d': '近 30 天', '1y': '近 1 年' }[range] || range;
  }

  function updateRangeSummary() {
    var s = S.series;
    if (!s) return;
    var t = s.totals || {};
    var kwh = t.kwh;

    $('sLabel1').textContent = '区间峰值功率';
    $('sVal1').textContent = t.peak_w != null ? num(t.peak_w, 1) + ' W' : '--';
    $('sLabel2').textContent = '区间耗电';
    $('sVal2').textContent = kwh != null ? num(kwh, 3) + ' kWh' : '--';
    $('sLabel3').textContent = '区间电费';
    $('sVal3').textContent = money(kwh);

    var cov = (t.cov || 0) * 100;
    var covText = cov >= 10 ? Math.round(cov) + '%'
      : cov >= 1 ? cov.toFixed(1) + '%'
      : cov > 0 ? '<1%' : '0%';
    var badge = $('coverageBadge');
    badge.textContent = s.empty ? '无数据' : '覆盖 ' + covText;
    badge.className = 'pill ' + (cov >= 80 ? 'pill-dim' : cov >= 40 ? 'pill-amber' : 'pill-red');
    badge.title = '这段区间里实际采到的样本占总应收样本的比例。低于 80% 说明中间有停机或断连，图上对应位置会画成斜纹。';

    var metaBase = (s.level === 'min' ? '每分钟聚合' : s.level === 'hour' ? '每小时聚合' : '每日聚合') +
      ' · ' + s.points.length + ' 点 · 采样 ' + s.interval_seconds + 's';
    var firstData = null;
    for (var i = 0; i < s.points.length; i++) {
      if (s.points[i].p != null) { firstData = s.points[i]; break; }
    }
    if (s.empty) {
      $('chartMeta').textContent = '—';
    } else if ((t.cov || 0) < 0.3 && firstData) {
      $('chartMeta').textContent = metaBase + ' · 数据自 ' + window.P1ChartUtil.timeLabel(firstData.t, s.range) + ' 起';
    } else if (s.points.length < 3) {
      $('chartMeta').textContent = metaBase + '（数据积累中）';
    } else {
      $('chartMeta').textContent = metaBase;
    }
  }

  function initChart() {
    if (!window.P1Chart) return;
    S.chart = new window.P1Chart($('chart'));
    var tip = $('chartTip');
    S.chart.onHover = function (info) {
      if (!info) {
        tip.hidden = true;
        return;
      }
      var p = info.point;
      var stage = tip.parentElement.getBoundingClientRect();
      var unit = info.unit === 'kW' ? 'kW' : 'W';
      var k = info.unit === 'kW' ? 0.001 : 1;
      var html = '<div class="t-head">' + window.P1ChartUtil.fullTime(p.t, S.series ? S.series.range : '24h') + '</div>';
      if (p.p == null) {
        html += '<div class="t-row t-gap"><span>采样中断</span><b>覆盖率 ' + Math.round(p.cov * 100) + '%</b></div>';
      } else {
        html += '<div class="t-row"><span>平均功率</span><b>' + (p.p * k).toFixed(2) + ' ' + unit + '</b></div>';
        html += '<div class="t-row"><span>峰值功率</span><b>' + (p.pmax * k).toFixed(2) + ' ' + unit + '</b></div>';
        html += '<div class="t-row"><span>本段电量</span><b>' + (p.e * 1000).toFixed(2) + ' Wh</b></div>';
        if (p.cov < 0.8) {
          html += '<div class="t-row t-gap"><span>数据覆盖</span><b>' + Math.round(p.cov * 100) + '%</b></div>';
        }
      }
      tip.innerHTML = html;
      var x = info.x;
      var maxX = stage.width - 8;
      tip.hidden = false;
      var tw = tip.offsetWidth;
      x = Math.min(maxX - tw / 2, Math.max(tw / 2 + 8, x));
      tip.style.left = x + 'px';
      tip.style.top = Math.max(46, info.y) + 'px';
    };

    if (window.ResizeObserver) {
      new ResizeObserver(function () { if (S.chart) S.chart.render(); })
        .observe(document.querySelector('.chart-stage'));
    } else {
      window.addEventListener('resize', function () { if (S.chart) S.chart.render(); });
    }
  }

  // ------------------------------------------------------------------
  // 渲染：定时 / 倒计时 / 事件
  // ------------------------------------------------------------------

  function renderTimers() {
    var data = S.timers;
    var el = $('timerList');
    if (!data) {
      el.innerHTML = '<div class="timer-empty">加载中…</div>';
      return;
    }
    var labels = (S.config && S.config.socket_labels) || [];
    var html = '';
    (data.sockets || []).forEach(function (g, i) {
      var timers = g.timers || [];
      var body;
      if (g.error) {
        body = '<div class="timer-empty">读取失败：' + esc(g.error) + '</div>';
      } else if (!timers.length) {
        body = '<div class="timer-empty">没有定时</div>';
      } else {
        body = timers.map(function (t) {
          return '<div class="timer-item">' +
            '<span class="timer-time">' + pad(t.hour) + ':' + pad(t.minute) + '</span>' +
            '<span class="timer-act ' + (t.action ? 'on' : 'off') + '">' + (t.action ? '开' : '关') + '</span>' +
            '<span class="timer-repeat">' + esc(t.days.text) + '</span>' +
            '<button class="timer-del" type="button" data-del-timer="' + i + '" data-time-utc="' + t.time_utc + '" data-repeat="' + t.repeat + '">删除</button>' +
            '</div>';
        }).join('');
      }
      html += '<div class="timer-group' + (timers.length ? '' : ' compact') + '">' +
        '<div class="timer-group-head">' +
          '<span class="g-code">S' + (i + 1) + '</span>' +
          '<span class="g-name">' + esc(labels[i] || ('插口 ' + (i + 1))) + '</span>' +
          '<span class="g-count">' + timers.length + ' 条</span>' +
        '</div>' + body +
        '</div>';
    });
    el.innerHTML = html;
  }

  function renderCountdowns() {
    var data = S.countdowns;
    var el = $('countdownList');
    if (!data) {
      el.innerHTML = '<div class="cd-empty">加载中…</div>';
      return;
    }
    var labels = (S.config && S.config.socket_labels) || [];
    var elapsed = Math.floor((Date.now() - S.cdFetchedAt) / 1000);
    var html = '';
    (data.sockets || []).forEach(function (g, i) {
      var cd = g.countdown;
      if (!cd) {
        html += '<div class="cd-item">' +
          '<span class="cd-code">S' + (i + 1) + '</span>' +
          '<span class="cd-name">' + esc(labels[i] || '') + '</span>' +
          '<span class="cd-act">无倒计时</span>' +
          '</div>';
        return;
      }
      var remain = Math.max(0, cd.remain - elapsed);
      html += '<div class="cd-item">' +
        '<span class="cd-code">S' + (i + 1) + '</span>' +
        '<span class="cd-name">' + esc(labels[i] || '') + '</span>' +
        '<span class="cd-act">' + (cd.action ? '后开启' : '后关闭') + '</span>' +
        '<span class="cd-remain" data-cd-remain="' + i + '" data-base="' + cd.remain + '">' + fmtRemain(remain) + '</span>' +
        '<button class="cd-cancel" type="button" data-cancel-cd="' + i + '">取消</button>' +
        '</div>';
    });
    el.innerHTML = html || '<div class="cd-empty">暂无倒计时</div>';
  }

  function tickCountdowns() {
    var elapsed = Math.floor((Date.now() - S.cdFetchedAt) / 1000);
    $$('[data-cd-remain]').forEach(function (el) {
      var base = parseInt(el.getAttribute('data-base'), 10);
      if (!isNaN(base)) el.textContent = fmtRemain(Math.max(0, base - elapsed));
    });
  }

  var KIND_CLASS = { switch: 'k-switch', link: 'k-link', timer: 'k-timer', countdown: 'k-countdown', system: 'k-system' };

  function eventListHtml(events) {
    if (!events || !events.length) return '<li><span class="event-detail">暂无事件</span></li>';
    return events.map(function (e) {
      return '<li>' +
        '<span class="event-kind ' + (KIND_CLASS[e.kind] || 'k-system') + '"></span>' +
        '<span class="event-time">' + fmtAgo(e.ts) + '</span>' +
        '<span class="event-detail">' + esc(e.detail) + '</span>' +
        '</li>';
    }).join('');
  }

  function renderEvents() {
    $('eventList').innerHTML = eventListHtml(S.events.slice(0, 8));
  }

  function renderInfo() {
    var d = S.device;
    var st = S.state || {};
    var el = $('infoGrid');
    if (!d) {
      el.innerHTML = '<div class="info-item"><span class="k">状态</span><span class="v">读取中…</span></div>';
      return;
    }
    var info = d.info || {};
    var wifi = d.wifi || {};
    var ver = d.version || {};
    var mem = st.def_st === 2 ? '来电自启' : st.def_st === 1 ? '保持关闭' : '未知';

    var rows = [
      ['设备名', info.name || '—', ''],
      ['序列号', info.sn || '—', 'mono'],
      ['MAC', info.mac || '—', 'mono'],
      ['固件', ver.version || info.version || '—', ''],
      ['IP', wifi.ip || (S.config && S.config.host) || '—', 'mono'],
      ['Wi-Fi', wifi.ssid || '—', ''],
      ['网关', wifi.gw || '—', 'mono'],
      ['断电记忆', mem, ''],
      ['指示灯', st.led ? '开启' : '关闭', ''],
      ['采样间隔', ((S.config && S.config.interval_seconds) || '—') + ' 秒', '']
    ];
    if (!d.reachable) {
      rows.unshift(['连接', '离线 · ' + (d.error || '未知原因'), '']);
    }
    el.innerHTML = rows.map(function (r) {
      return '<div class="info-item"><span class="k">' + esc(r[0]) + '</span>' +
        '<span class="v ' + r[2] + '" title="' + esc(r[1]) + '">' + esc(r[1]) + '</span></div>';
    }).join('');
  }

  // ------------------------------------------------------------------
  // 轮询
  // ------------------------------------------------------------------

  function applyState(st) {
    S.state = st;
    updateSockets();
    updateMetrics();
  }

  function pollState() {
    return api('/api/state').then(applyState).catch(function (e) {
      var lb = $('linkBadge');
      lb.className = 'link-badge offline';
      $('linkText').textContent = '服务不可达';
    });
  }

  function pollSummary() {
    return api('/api/summary').then(function (d) {
      S.summary = d;
      updateSummary();
    }).catch(function () {});
  }

  function pollSeries() {
    return api('/api/series?range=' + encodeURIComponent(S.range)).then(function (d) {
      S.series = d;
      renderChart();
    }).catch(function () {});
  }

  function pollTimers() {
    return api('/api/timers').then(function (d) {
      S.timers = d;
      renderTimers();
    }).catch(function () {});
  }

  function pollCountdowns() {
    return api('/api/countdowns').then(function (d) {
      S.countdowns = d;
      S.cdFetchedAt = Date.now();
      renderCountdowns();
    }).catch(function () {});
  }

  function pollEvents() {
    return api('/api/events?limit=40').then(function (d) {
      S.events = d.events || [];
      renderEvents();
    }).catch(function () {});
  }

  function pollDevice() {
    return api('/api/device').then(function (d) {
      S.device = d;
      renderInfo();
    }).catch(function () {});
  }

  // ------------------------------------------------------------------
  // 动作
  // ------------------------------------------------------------------

  function withBusy(fn) {
    return function () {
      if (S.busy) return Promise.resolve();
      S.busy = true;
      return Promise.resolve(fn.apply(null, arguments)).finally(function () {
        S.busy = false;
      });
    };
  }

  var toggleAll = withBusy(function (status) {
    return api('/api/switch', { method: 'POST', body: { scope: 'all', status: status } })
      .then(function (r) { toast(r.message || '已执行', 'ok'); })
      .catch(function (e) { toast(e.message, 'err'); })
      .then(function () { return pollState(); });
  });

  var toggleOne = withBusy(function (index) {
    var sockets = (S.state && S.state.sockets) || [0, 0, 0, 0];
    var status = sockets[index] ? 0 : 1;
    return api('/api/switch', { method: 'POST', body: { scope: 'one', index: index, status: status } })
      .then(function (r) { toast(r.message || '已执行', 'ok'); })
      .catch(function (e) { toast(e.message, 'err'); })
      .then(function () { return pollState(); });
  });

  var runScene = withBusy(function (key) {
    var scene = SCENES[key];
    if (!scene) return Promise.resolve();
    return api('/api/switch_many', { method: 'POST', body: { name: scene.name, statuses: scene.states } })
      .then(function (r) { toast(r.message || ('已切换到' + scene.name), 'ok'); })
      .catch(function (e) { toast(e.message, 'err'); })
      .then(function () { return pollState(); });
  });

  function openModal(id) { $(id).hidden = false; }
  function closeModal(id) { $(id).hidden = true; }
  function closeAllModals() {
    $$('.modal').forEach(function (m) { m.hidden = true; });
  }

  // ---- 插口标签 ----
  var editingSocket = -1;
  function openLabelModal(index) {
    editingSocket = index;
    var labels = (S.config && S.config.socket_labels) || [];
    $('labelTitle').textContent = '修改 S' + (index + 1) + ' 名称';
    $('labelInput').value = labels[index] || '';
    openModal('modalLabel');
    setTimeout(function () { $('labelInput').focus(); }, 50);
  }

  function saveLabel() {
    var value = $('labelInput').value.trim();
    api('/api/socket_label', { method: 'POST', body: { index: editingSocket, label: value } })
      .then(function (r) {
        if (S.config) S.config.socket_labels = r.labels;
        closeModal('modalLabel');
        updateSockets();
        renderTimers();
        renderCountdowns();
        toast('名称已保存', 'ok');
      })
      .catch(function (e) { toast(e.message, 'err'); });
  }

  // ---- 设备名 ----
  function saveDeviceName() {
    var value = $('deviceNameInput').value.trim();
    if (!value) { toast('设备名不能为空', 'err'); return; }
    api('/api/device_name', { method: 'POST', body: { name: value } })
      .then(function () {
        closeModal('modalDeviceName');
        toast('已写入插座，官方 App 也会同步', 'ok');
        pollDevice();
      })
      .catch(function (e) { toast(e.message, 'err'); });
  }

  // ---- 定时 ----
  var repeatValue = 127;

  function buildWeekPicker() {
    var html = '';
    for (var i = 0; i < 7; i++) {
      html += '<button type="button" data-day="' + i + '">' + REPEAT_LABELS[i] + '</button>';
    }
    $('weekPicker').innerHTML = html;
  }

  function syncWeekPicker() {
    $$('#weekPicker button').forEach(function (btn) {
      var day = parseInt(btn.getAttribute('data-day'), 10);
      btn.classList.toggle('on', !!((repeatValue >> day) & 1));
    });
    $$('#repeatPresets button').forEach(function (btn) {
      btn.classList.toggle('active', parseInt(btn.getAttribute('data-repeat'), 10) === repeatValue);
    });
  }

  function fillSocketSelect(sel) {
    var labels = (S.config && S.config.socket_labels) || [];
    sel.innerHTML = labels.map(function (label, i) {
      return '<option value="' + i + '">S' + (i + 1) + ' · ' + esc(label) + '</option>';
    }).join('');
  }

  function openTimerModal(index) {
    fillSocketSelect($('timerSocket'));
    if (index != null) $('timerSocket').value = String(index);
    repeatValue = 127;
    syncWeekPicker();
    openModal('modalTimer');
  }

  function saveTimer() {
    var body = {
      op: 'add',
      index: parseInt($('timerSocket').value, 10),
      action: parseInt($('timerAction').value, 10),
      repeat: repeatValue
    };
    var time = $('timerTime').value || '07:30';
    var parts = time.split(':');
    body.hour = parseInt(parts[0], 10);
    body.minute = parseInt(parts[1], 10);

    api('/api/timer', { method: 'POST', body: body })
      .then(function (r) {
        closeModal('modalTimer');
        S.timers = r.timers;
        renderTimers();
        toast(r.message || '定时已写入插座', 'ok');
        pollEvents();
      })
      .catch(function (e) { toast(e.message, 'err'); });
  }

  function deleteTimer(index, timeUtc, repeat) {
    if (!confirm('确定删除这条定时吗？（会从插座里删掉）')) return;
    api('/api/timer', { method: 'POST', body: { op: 'del', index: index, time_utc: timeUtc, repeat: repeat } })
      .then(function (r) {
        S.timers = r.timers;
        renderTimers();
        toast('定时已删除', 'ok');
        pollEvents();
      })
      .catch(function (e) { toast(e.message, 'err'); });
  }

  // ---- 倒计时 ----
  function openCountdownModal(index) {
    fillSocketSelect($('cdSocket'));
    if (index != null) $('cdSocket').value = String(index);
    $('cdValue').value = '30';
    $('cdUnit').value = '60';
    $('cdAction').value = '0';
    updateCdHint();
    openModal('modalCountdown');
  }

  function updateCdHint() {
    var v = parseFloat($('cdValue').value) || 0;
    var unit = parseInt($('cdUnit').value, 10);
    var total = v * unit;
    var act = $('cdAction').value === '1' ? '开启' : '关闭';
    var label = (S.config && S.config.socket_labels || [])[parseInt($('cdSocket').value, 10)] || '';
    $('cdHint').textContent = total > 0
      ? '将在 ' + fmtRemain(total) + ' 后' + act + ' ' + label
      : '请填写大于 0 的时长';
  }

  function saveCountdown() {
    var v = parseFloat($('cdValue').value) || 0;
    var unit = parseInt($('cdUnit').value, 10);
    var seconds = Math.round(v * unit);
    if (seconds < 1) { toast('时长必须大于 0', 'err'); return; }
    if (seconds > 604800) { toast('最长 7 天', 'err'); return; }
    api('/api/countdown', {
      method: 'POST',
      body: {
        op: 'add',
        index: parseInt($('cdSocket').value, 10),
        action: parseInt($('cdAction').value, 10),
        seconds: seconds
      }
    }).then(function () {
      closeModal('modalCountdown');
      toast('倒计时已下发', 'ok');
      pollCountdowns();
      pollEvents();
    }).catch(function (e) { toast(e.message, 'err'); });
  }

  function cancelCountdown(index) {
    api('/api/countdown', { method: 'POST', body: { op: 'del', index: index } })
      .then(function () {
        toast('倒计时已取消', 'ok');
        pollCountdowns();
        pollEvents();
      })
      .catch(function (e) { toast(e.message, 'err'); });
  }

  // ---- 设置 ----
  function openSettings() {
    var c = S.config || {};
    $('setHost').value = c.host || '';
    $('setInterval').value = c.interval_seconds || 2;
    $('setPrice').value = c.price_per_kwh || 0.6;
    $('setRated').value = c.rated_watts || 2500;
    $('setToken').value = '';
    $('setToken').placeholder = c.auth_enabled ? '已设置，留空保持不变' : '留空表示不校验';
    $('setTokenClear').checked = false;
    $('discoverHint').textContent = '';
    openModal('modalSettings');
  }

  function saveSettings() {
    var body = {
      host: $('setHost').value.trim(),
      interval_seconds: parseInt($('setInterval').value, 10),
      price_per_kwh: parseFloat($('setPrice').value),
      rated_watts: parseInt($('setRated').value, 10)
    };
    // 访问密码：只有真的填了或者明确要清除时才提交这个字段，
    // 否则"留空"会被当成"把密码清掉"，用户一保存就把自己锁在门外。
    var newToken = $('setToken').value.trim();
    var clearToken = $('setTokenClear').checked;
    var tokenChanged = false;
    if (newToken) {
      body.access_token = newToken;
      tokenChanged = true;
    } else if (clearToken) {
      body.access_token = '';
      tokenChanged = true;
    }

    api('/api/config', { method: 'POST', body: body })
      .then(function (r) {
        S.config = r.config;
        S.price = r.config.price_per_kwh;
        S.rated = r.config.rated_watts;
        if (tokenChanged) {
          try {
            if (newToken) { localStorage.setItem('p1pro_token', newToken); }
            else { localStorage.removeItem('p1pro_token'); }
          } catch (e) {}
          closeModal('modalSettings');
          toast(r.message || '已保存', 'ok');
          // 口令变了，必须换新口令重新加载，否则接下来的请求全是 401
          setTimeout(function () { location.replace('/'); }, 700);
          return;
        }
        closeModal('modalSettings');
        toast(r.message || '已保存', 'ok');
        pollState();
        pollDevice();
        pollSeries();
        pollSummary();
      })
      .catch(function (e) { toast(e.message, 'err'); });
  }

  function discover() {
    var btn = $('btnDiscover');
    btn.disabled = true;
    $('discoverHint').textContent = '扫描中（约 5 秒）…';
    api('/api/discover', { method: 'POST', body: {} })
      .then(function (r) {
        var found = r.found || [];
        if (!found.length) {
          $('discoverHint').textContent = '没找到插线板（网段 ' + r.cidr + '）';
          return;
        }
        var first = found[0];
        $('setHost').value = first.ip;
        $('discoverHint').textContent = '找到 ' + found.length + ' 台：' +
          found.map(function (f) { return f.ip + ' (' + f.sn + ')'; }).join('、');
        toast('已填入 ' + first.ip, 'ok');
      })
      .catch(function (e) { $('discoverHint').textContent = '扫描失败：' + e.message; })
      .finally(function () { btn.disabled = false; });
  }

  // ------------------------------------------------------------------
  // 绑定
  // ------------------------------------------------------------------

  function bind() {
    document.addEventListener('click', function (ev) {
      var t = ev.target;

      var toggle = t.closest('[data-toggle]');
      if (toggle) {
        toggleOne(parseInt(toggle.getAttribute('data-toggle'), 10));
        return;
      }
      var rename = t.closest('[data-rename]');
      if (rename) {
        openLabelModal(parseInt(rename.getAttribute('data-rename'), 10));
        return;
      }
      var delTimer = t.closest('[data-del-timer]');
      if (delTimer) {
        deleteTimer(
          parseInt(delTimer.getAttribute('data-del-timer'), 10),
          parseInt(delTimer.getAttribute('data-time-utc'), 10),
          parseInt(delTimer.getAttribute('data-repeat'), 10)
        );
        return;
      }
      var cancelCd = t.closest('[data-cancel-cd]');
      if (cancelCd) {
        cancelCountdown(parseInt(cancelCd.getAttribute('data-cancel-cd'), 10));
        return;
      }
      var scene = t.closest('[data-scene]');
      if (scene) {
        runScene(scene.getAttribute('data-scene'));
        return;
      }
      if (t.closest('[data-close]')) {
        closeAllModals();
      }
    });

    $$('.modal').forEach(function (m) {
      m.addEventListener('click', function (ev) {
        if (ev.target === m) closeAllModals();
      });
    });

    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') closeAllModals();
    });

    $('btnAllOn').addEventListener('click', function () { toggleAll(1); });
    $('btnAllOff').addEventListener('click', function () { toggleAll(0); });
    $('btnMaster').addEventListener('click', function () {
      var sockets = (S.state && S.state.sockets) || [0, 0, 0, 0];
      toggleAll(sockets.some(function (v) { return v; }) ? 0 : 1);
    });

    $('btnRefresh').addEventListener('click', function () {
      pollState(); pollSummary(); pollSeries(); pollDevice();
      pollTimers(); pollCountdowns(); pollEvents();
      toast('已刷新', 'ok');
    });

    $('btnExport').addEventListener('click', function () {
      var url = '/api/export?type=minute&days=7' + (TOKEN ? '&token=' + encodeURIComponent(TOKEN) : '');
      window.location.href = url;
      toast('正在导出最近 7 天的分钟级数据', 'ok');
    });

    $('btnSettings').addEventListener('click', openSettings);
    $('btnSaveSettings').addEventListener('click', saveSettings);
    $('btnDiscover').addEventListener('click', discover);
    $('btnRenameDevice').addEventListener('click', function () {
      var name = (S.device && S.device.info && S.device.info.name) || '';
      $('deviceNameInput').value = name;
      openModal('modalDeviceName');
      setTimeout(function () { $('deviceNameInput').focus(); }, 50);
    });
    $('btnSaveDeviceName').addEventListener('click', saveDeviceName);
    $('btnSaveLabel').addEventListener('click', saveLabel);

    $('btnAddTimer').addEventListener('click', function () { openTimerModal(null); });
    $('btnReloadTimers').addEventListener('click', function () {
      pollTimers().then(function () { toast('已刷新定时', 'ok'); });
    });
    $('btnSaveTimer').addEventListener('click', saveTimer);
    $('btnAddCountdown').addEventListener('click', function () { openCountdownModal(null); });
    $('btnSaveCountdown').addEventListener('click', saveCountdown);

    ['cdValue', 'cdUnit', 'cdAction', 'cdSocket'].forEach(function (id) {
      $(id).addEventListener('input', updateCdHint);
      $(id).addEventListener('change', updateCdHint);
    });

    $('btnEvents').addEventListener('click', function () {
      $('eventListFull').innerHTML = eventListHtml(S.events);
      openModal('modalEvents');
    });

    $('btnAlertClose').addEventListener('click', function () {
      $('alertBar').hidden = true;
    });

    $('repeatPresets').addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-repeat]');
      if (!btn) return;
      repeatValue = parseInt(btn.getAttribute('data-repeat'), 10);
      syncWeekPicker();
    });

    $('weekPicker').addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-day]');
      if (!btn) return;
      var day = parseInt(btn.getAttribute('data-day'), 10);
      repeatValue = repeatValue ^ (1 << day);
      syncWeekPicker();
    });

    $('rangeTabs').addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-range]');
      if (!btn) return;
      var r = btn.getAttribute('data-range');
      if (r === S.range) return;
      S.range = r;
      $$('#rangeTabs button').forEach(function (b) { b.classList.toggle('active', b === btn); });
      pollSeries();
    });

    var priceTimer = null;
    $('inputPrice').addEventListener('input', function () {
      var v = parseFloat($('inputPrice').value);
      if (isNaN(v) || v < 0 || v > 20) return;
      S.price = v;
      updateSummary();
      updateRangeSummary();
      clearTimeout(priceTimer);
      priceTimer = setTimeout(function () {
        api('/api/config', { method: 'POST', body: { price_per_kwh: Number(v.toFixed(4)) } })
          .then(function (r) { S.config = r.config; })
          .catch(function () {});
      }, 900);
    });
  }

  // ------------------------------------------------------------------
  // 启动
  // ------------------------------------------------------------------

  function boot() {
    buildSockets();
    buildWeekPicker();
    initChart();
    bind();

    api('/api/bootstrap').then(function (d) {
      S.boot = d;
      S.config = d.config;
      S.price = d.config.price_per_kwh;
      S.rated = d.config.rated_watts;
      S.range = d.config.default_range || '24h';
      $('inputPrice').value = Number(S.price).toFixed(2);
      $('tagModel').textContent = '飞牛版 v' + d.app.version;
      $('footVersion').textContent = 'P1Pro 控制台（飞牛版）v' + d.app.version;
      $('footStorage').textContent = fmtBytes((d.storage || {}).db_bytes) + ' · ' +
        ((d.storage || {}).samples || 0).toLocaleString() + ' 条原始样本';

      $$('#rangeTabs button').forEach(function (b) {
        b.classList.toggle('active', b.getAttribute('data-range') === S.range);
      });

      S.summary = d.summary;
      applyState(d.state);
      updateSummary();

      if (!d.state.online) {
        $('alertBar').hidden = false;
        $('alertPill').textContent = '离线';
        $('alertText').textContent = '暂时连不上插座：' + (d.state.error || '未知原因') +
          '。面板仍可查看历史数据，检查 IP 与网络后会自动恢复。';
      }

      return Promise.all([pollSeries(), pollTimers(), pollCountdowns(), pollEvents(), pollDevice()]);
    }).catch(function (e) {
      toast('初始化失败：' + e.message, 'err');
      $('linkBadge').className = 'link-badge offline';
      $('linkText').textContent = '服务不可达';
    });

    // 状态：1.5 秒（采样 2 秒，比采样略快一点就能及时看到变化）
    setInterval(pollState, 1500);
    setInterval(pollSummary, 20000);
    setInterval(pollSeries, 30000);
    setInterval(pollCountdowns, 8000);
    setInterval(pollEvents, 45000);
    setInterval(pollDevice, 60000);
    setInterval(tickCountdowns, 1000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
