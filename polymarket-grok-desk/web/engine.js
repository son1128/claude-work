/* Grok Desk replay engine — a line-for-line port of polymarket-grok-desk/grokdesk
   (quant.py, agents.py, risk.py, ledger.py) driven by recorded market data. */
var GD = (function () {
  "use strict";

  var DEFAULTS = {
    bankroll: 500,
    volLookback: 300, trendLookback: 90, r2Min: 0.35, driftWeight: 0.0,
    maxDriftBpsMin: 5, basisBps: 2, volFloorBpsMin: 1,
    minEdge: 0.04, slippageTicks: 1, minPrice: 0.05, maxPrice: 0.95, maxGap: 0.30,
    kellyMult: 0.25, maxBankrollFrac: 0.02, maxStake: 25, assumedDepth: 400, maxDepthFrac: 0.25,
    maxDailyLoss: 50, minSecondsLeft: 20, maxSecondsLeft: 600,
    spread: 0.01, tick: 0.01, minShares: 5, twap: 60,
    feeOverride: null,
    supervisor: "rules", volSpikeX: 3.0
  };

  function normCdf(x) {
    // Abramowitz–Stegun 7.1.26 erf, |err| < 1.5e-7
    var s = x < 0 ? -1 : 1, z = Math.abs(x) / Math.SQRT2;
    var t = 1 / (1 + 0.3275911 * z);
    var y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-z * z);
    return 0.5 * (1 + s * y);
  }

  function regression(xs, ys) {
    var n = xs.length, i;
    if (n < 3) return { slope: 0, r2: 0, n: n };
    var mx = 0, my = 0;
    for (i = 0; i < n; i++) { mx += xs[i]; my += ys[i]; }
    mx /= n; my /= n;
    var sxx = 0, syy = 0, sxy = 0;
    for (i = 0; i < n; i++) {
      var dx = xs[i] - mx, dy = ys[i] - my;
      sxx += dx * dx; syy += dy * dy; sxy += dx * dy;
    }
    if (sxx === 0) return { slope: 0, r2: 0, n: n };
    var r2 = syy === 0 ? 0 : Math.max(0, Math.min(1, (sxy * sxy) / (sxx * syy)));
    return { slope: sxy / sxx, r2: r2, n: n };
  }

  // 1-second samples -> sigma of log price per sqrt(second)
  function realizedVol(px, from, to) {
    var sum = 0, n = 0;
    for (var i = from + 1; i <= to; i++) {
      var r = Math.log(px[i] / px[i - 1]);
      sum += r * r; n++;
    }
    return n < 2 ? 0 : Math.sqrt(sum / n);
  }

  function clip(p) { return Math.max(0.01, Math.min(0.99, p)); }

  function probUp(s, k, tau, sig, mu, L, realized, basisBps) {
    tau = Math.max(0, tau); sig = Math.max(0, sig); L = Math.max(1e-9, L);
    var basis = basisBps / 10000;
    if (tau > L) {
      var mean = Math.log(s / k) + mu * (tau - L / 2);
      var v = sig * sig * ((tau - L) + L / 3) + basis * basis;
      if (v <= 0) return clip(mean >= 0 ? 1 : 0);
      return clip(normCdf(mean / Math.sqrt(v)));
    }
    var rz = realized == null ? s : realized;
    var mAvg = ((L - tau) * rz + tau * s * (1 + mu * tau / 2)) / L;
    var sd = Math.sqrt((s * sig) * (s * sig) * tau * tau * tau / 3) / L;
    sd = Math.sqrt(sd * sd + (k * basis) * (k * basis));
    if (sd <= 0) return clip(mAvg >= k ? 1 : 0);
    return clip(normCdf((mAvg - k) / sd));
  }

  function feePerShare(rate, exp, p) { return rate * Math.pow(p * (1 - p), exp); }
  function kelly(p, c) { return (c <= 0 || c >= 1) ? 0 : Math.max(0, (p - c) / (1 - c)); }
  function ceilTick(p, tick) { return Math.round(Math.ceil(p / tick - 1e-9) * tick * 10000) / 10000; }

  // ---- data ---------------------------------------------------------------
  function decode(raw) {
    var ws = raw.windows.map(function (w) {
      var px = new Float64Array(w.spot.length), acc = 0;
      for (var i = 0; i < w.spot.length; i++) { acc += w.spot[i]; px[i] = acc / 100; }
      return { start: w.start, up: w.up, q: w.q, fee: w.fee, px: px, mkt: w.mkt };
    });
    return { generated: raw.generated, look: raw.lookback, step: raw.step, windows: ws };
  }

  // ---- PRIOR ----------------------------------------------------------------
  function prior(P, w, look, off) {
    var idx = look + off, px = w.px;
    var vFrom = Math.max(0, idx - P.volLookback);
    var sigma = realizedVol(px, vFrom, idx);
    var floor = P.volFloorBpsMin / 10000 / Math.sqrt(60);
    if (sigma < floor) sigma = floor;
    var tFrom = Math.max(0, idx - P.trendLookback), xs = [], ys = [];
    for (var i = tFrom; i <= idx; i++) { xs.push(i - idx); ys.push(Math.log(px[i])); }
    var reg = regression(xs, ys), drift = 0;
    if (reg.r2 >= P.r2Min) {
      var cap = P.maxDriftBpsMin / 10000 / 60;
      drift = Math.max(-cap, Math.min(cap, reg.slope * P.driftWeight));
    }
    var left = 900 - off, realized = null;
    if (left < P.twap) {
      var s = 0, n = 0;
      for (var j = look + 900 - P.twap; j <= idx; j++) { s += px[j]; n++; }
      realized = n ? s / n : null;
    }
    var k = px[look], spot = px[idx];
    // short-horizon vol for the supervisor's spike check
    var sigma30 = realizedVol(px, Math.max(0, idx - 30), idx);
    return {
      pUp: probUp(spot, k, left, sigma, drift, P.twap, realized, P.basisBps),
      pUpNoDrift: probUp(spot, k, left, sigma, 0, P.twap, realized, P.basisBps),
      spot: spot, open: k, left: left, sigma: sigma, sigma30: sigma30,
      slope: reg.slope, r2: reg.r2, drift: drift
    };
  }

  // ---- EDGE -----------------------------------------------------------------
  function edge(P, w, sig, mid) {
    var rate = P.feeOverride != null ? P.feeOverride : w.fee[0], exp = w.fee[1];
    var sides = [
      { side: "UP", pWin: sig.pUp, ask: ceilTick(mid + P.spread / 2, P.tick) },
      { side: "DOWN", pWin: 1 - sig.pUp, ask: ceilTick(1 - mid + P.spread / 2, P.tick) }
    ];
    var best = null;
    sides.forEach(function (o) {
      o.limit = Math.min(0.99, Math.round((o.ask + P.slippageTicks * P.tick) * 10000) / 10000);
      o.fee = feePerShare(rate, exp, o.limit);
      o.cost = o.limit + o.fee;
      o.edge = o.pWin - o.cost;
      if (o.ask < P.minPrice || o.ask > P.maxPrice) o.skip = "가격 범위 밖";
      else if (Math.abs(o.pWin - o.ask) > P.maxGap) o.skip = "모델·시장 괴리 과대";
      else if (o.edge < P.minEdge) o.skip = "우위 부족";
      if (!o.skip && (!best || o.edge > best.edge)) best = o;
    });
    return { best: best, sides: sides };
  }

  // ---- KELLY ----------------------------------------------------------------
  function size(P, opp, bankroll) {
    var f = kelly(opp.pWin, opp.cost);
    var stake = Math.min(bankroll * Math.min(f * P.kellyMult, P.maxBankrollFrac), P.maxStake);
    var shares = opp.cost > 0 ? stake / opp.cost : 0;
    shares = Math.min(shares, P.assumedDepth * P.maxDepthFrac);
    shares = Math.floor(shares * 100) / 100;
    if (shares < P.minShares) return { shares: 0, stake: 0, f: f, reason: "최소 주문 " + P.minShares + "주 미만" };
    return { shares: shares, stake: shares * opp.cost, f: f, reason: "ok" };
  }

  // ---- rule supervisor (what the Grok prompt checks, as deterministic rules) --
  function rulesSupervisor(P, sig, opp) {
    if (sig.sigma30 > P.volSpikeX * sig.sigma) return { approve: false, reason: "30초 변동성이 평소의 " + (sig.sigma30 / sig.sigma).toFixed(1) + "배", source: "rules" };
    if (opp.pWin > 0.97) return { approve: false, reason: "승률 97% 초과: 남는 몫이 작고 basis 위험이 큼", source: "rules" };
    return { approve: true, reason: "규칙 통과", source: "rules" };
  }

  // ---- the desk: an explicit event stepper so the UI can animate it ---------
  function Desk(data, params) {
    this.data = data;
    this.P = Object.assign({}, DEFAULTS, params || {});
    this.reset();
  }

  Desk.prototype.reset = function () {
    this.wi = 0; this.mi = 0;
    this.cash = this.P.bankroll;
    this.positions = []; this.trace = []; this.equity = [{ t: this.data.windows[0].start, v: this.cash }];
    this.brier = { n: 0, model: 0, market: 0, noDrift: 0 };
    this.done = false; this.last = null; this.pending = null;
    this.dayPnl = {};
  };

  Desk.prototype.log = function (kind, w, o) {
    o.kind = kind; o.window = this.wi; o.t = o.t || w.start;
    this.trace.push(o);
    if (this.trace.length > 4000) this.trace.shift();
  };

  // Next market observation, or the window's settlement. Returns an event.
  // If the supervisor is async (Claude), returns {needsReview, proposal} and
  // waits for resolve(verdict).
  Desk.prototype.next = function () {
    if (this.done) return null;
    if (this.pending) return { kind: "waiting" };
    var w = this.data.windows[this.wi];
    if (this.mi >= w.mkt.length) return this.settle(w);
    var pt = w.mkt[this.mi++], off = pt[0], mid = pt[1];
    var P = this.P, now = w.start + off, left = 900 - off;
    var sig = prior(P, w, this.data.look, off);
    var y = w.up ? 1 : 0;
    this.brier.n++;
    this.brier.model += (sig.pUp - y) * (sig.pUp - y);
    this.brier.noDrift += (sig.pUpNoDrift - y) * (sig.pUpNoDrift - y);
    this.brier.market += (mid - y) * (mid - y);
    var ev = { kind: "tick", t: now, off: off, mid: mid, sig: sig, w: w };
    this.last = ev;

    var held = this.positions.some(function (p) { return p.window === this.wi; }, this);
    var day = Math.floor(now / 86400), dayPnl = this.dayPnl[day] || 0;
    var block = null;
    if (dayPnl <= -Math.abs(P.maxDailyLoss)) block = "일일 손실 한도";
    else if (held) block = "이 시장에 이미 포지션 있음";
    else if (left < P.minSecondsLeft) block = "만기 직전";
    else if (left > P.maxSecondsLeft) block = "창 초반 (정보 부족)";
    ev.block = block;
    if (block) { ev.stage = "RISK"; return ev; }

    var e = edge(P, w, sig, mid);
    ev.sides = e.sides;
    if (!e.best) {
      ev.stage = "EDGE";
      this.log("no_edge", w, { t: now, pUp: sig.pUp, mid: mid, note: e.sides.map(function (s) { return s.side + " " + (s.edge >= 0 ? "+" : "") + s.edge.toFixed(3) + " " + (s.skip || ""); }).join(" · ") });
      return ev;
    }
    var sz = size(P, e.best, this.cash);
    ev.opp = e.best; ev.size = sz;
    if (!sz.shares) { ev.stage = "KELLY"; this.log("size_skip", w, { t: now, note: sz.reason }); return ev; }

    var proposal = { opp: e.best, size: sz, sig: sig, mid: mid, now: now, left: left, w: w };
    if (P.supervisor === "manual") {
      this.pending = proposal;
      ev.stage = "SUPERVISOR"; ev.needsReview = true; ev.proposal = proposal;
      return ev;
    }
    var verdict = P.supervisor === "off" ? { approve: true, reason: "감독관 꺼짐", source: "off" } : rulesSupervisor(P, sig, e.best);
    return this.decide(proposal, verdict, ev);
  };

  Desk.prototype.resolve = function (verdict) {
    var pr = this.pending;
    if (!pr) return null;
    this.pending = null;
    var ev = { kind: "tick", t: pr.now, off: pr.now - pr.w.start, mid: pr.mid, sig: pr.sig, w: pr.w, opp: pr.opp, size: pr.size };
    return this.decide(pr, verdict, ev);
  };

  Desk.prototype.decide = function (pr, verdict, ev) {
    ev.verdict = verdict;
    this.log("supervisor", pr.w, { t: pr.now, note: (verdict.approve ? "승인" : "거부") + " · " + verdict.reason, source: verdict.source });
    if (!verdict.approve) { ev.stage = "SUPERVISOR"; return ev; }
    var o = pr.opp, s = pr.size;
    var fee = s.shares * o.fee;
    var pos = {
      id: this.positions.length + 1, window: this.wi, q: pr.w.q, side: o.side, shares: s.shares,
      price: o.limit, fee: fee, cost: s.shares * o.limit + fee, pWin: o.pWin, edge: o.edge,
      t: pr.now, left: pr.left, status: "open", pnl: 0
    };
    this.cash -= pos.cost;
    this.positions.push(pos);
    ev.stage = "TAKER"; ev.fill = pos;
    this.log("fill", pr.w, { t: pr.now, note: o.side + " " + s.shares + "주 @ " + o.limit.toFixed(2) + " · p=" + o.pWin.toFixed(3) + " · 우위 " + o.edge.toFixed(3) });
    return ev;
  };

  Desk.prototype.settle = function (w) {
    var end = w.start + 900, settled = [];
    this.positions.forEach(function (p) {
      if (p.window !== this.wi || p.status !== "open") return;
      var won = (p.side === "UP") === w.up;
      p.status = won ? "won" : "lost";
      p.payout = won ? p.shares : 0;
      p.pnl = p.payout - p.cost;
      this.cash += p.payout;
      var day = Math.floor(end / 86400);
      this.dayPnl[day] = (this.dayPnl[day] || 0) + p.pnl;
      settled.push(p);
      this.log("settle", w, { t: end, note: p.side + " → " + (won ? "적중" : "실패") + " " + (p.pnl >= 0 ? "+" : "") + p.pnl.toFixed(2) + "달러" });
    }, this);
    this.equity.push({ t: end, v: this.cash });
    var ev = { kind: "settle", t: end, w: w, settled: settled, stage: "CLOSER" };
    this.wi++; this.mi = 0;
    if (this.wi >= this.data.windows.length) this.done = true;
    this.last = ev;
    return ev;
  };

  Desk.prototype.stats = function () {
    var done = this.positions.filter(function (p) { return p.status !== "open"; });
    var wins = done.filter(function (p) { return p.status === "won"; }).length;
    var sum = function (a, f) { return a.reduce(function (s, p) { return s + f(p); }, 0); };
    var staked = sum(done, function (p) { return p.cost; });
    var b = this.brier;
    return {
      trades: this.positions.length, settled: done.length, wins: wins,
      winRate: done.length ? wins / done.length : null,
      pnl: sum(done, function (p) { return p.pnl; }),
      expPnl: sum(done, function (p) { return p.shares * p.pWin - p.cost; }),
      fees: sum(this.positions, function (p) { return p.fee; }),
      roi: staked ? sum(done, function (p) { return p.pnl; }) / staked : null,
      bankroll: this.cash + sum(this.positions.filter(function (p) { return p.status === "open"; }), function (p) { return p.cost; }),
      cash: this.cash,
      brierModel: b.n ? b.model / b.n : null, brierMarket: b.n ? b.market / b.n : null,
      brierNoDrift: b.n ? b.noDrift / b.n : null, obs: b.n
    };
  };

  Desk.prototype.runToEnd = function () {
    var guard = 0, sv = this.P.supervisor;
    if (sv === "manual") this.P.supervisor = "rules";
    while (!this.done && guard++ < 1e6) {
      var ev = this.next();
      if (ev && ev.needsReview) this.resolve({ approve: true, reason: "일괄 실행: 규칙 감독관으로 대체", source: "rules" });
    }
    this.P.supervisor = sv;
    return this.stats();
  };

  return {
    DEFAULTS: DEFAULTS, decode: decode, Desk: Desk,
    _t: { normCdf: normCdf, regression: regression, probUp: probUp, feePerShare: feePerShare, kelly: kelly, ceilTick: ceilTick, prior: prior }
  };
})();
if (typeof module !== "undefined") module.exports = GD;
