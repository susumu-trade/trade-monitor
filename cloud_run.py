# -*- coding: utf-8 -*-
"""
トレード監視 クラウド版 (Phase 6 / GitHub Actions)

1回の起動で「急変動・ニュース・経済指標カレンダー・売買シグナル」を順にチェックし、
新規イベントがあれば Telegram へ通知する。GitHub Actions の cron で5分ごとに実行する。

- 外部ライブラリ不要（標準ライブラリのみ）。指標計算も翻訳も自前。
- 秘密情報は環境変数: TG_BOT_TOKEN / TG_CHAT_ID （GitHub Secrets）
- 設定は cloud_config.json、状態は state/state.json（リポジトリに自動コミット保存）
- 通知先は Telegram のみ（クラウドにPCは無いため）
"""
import os, json, re, math, datetime, time, subprocess
import urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "cloud_config.json")
STATE_DIR = os.environ.get("STATE_DIR") or os.path.join(HERE, "state")
STATE = os.path.join(STATE_DIR, "state.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
JST = datetime.timezone(datetime.timedelta(hours=9))

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
CHAT = os.environ.get("TG_CHAT_ID", "")

def now_jst():
    return datetime.datetime.now(JST)

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def tg_send(text):
    if not TOKEN or not CHAT:
        print("[tg] TOKEN/CHAT 未設定のためスキップ")
        return False
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text}).encode()
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            return json.load(r).get("ok", False)
    except Exception as e:
        print(f"[tg] 送信失敗: {e}")
        return False

def translate(text, enabled):
    if not enabled or not text.strip():
        return text
    try:
        q = urllib.parse.quote(text)
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=ja&dt=t&q={q}"
        tr = json.loads(http_get(url, timeout=12).decode("utf-8"))
        return "".join(seg[0] for seg in tr[0]) or text
    except Exception:
        return text

def yahoo_chart(symbol, rng, interval):
    enc = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{enc}?range={rng}&interval={interval}"
    data = json.loads(http_get(url).decode("utf-8"))
    res = data["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    closes, highs, lows = [], [], []
    for c, h, l in zip(q.get("close") or [], q.get("high") or [], q.get("low") or []):
        if c is None:
            continue
        closes.append(c)
        highs.append(h if h is not None else c)
        lows.append(l if l is not None else c)
    return closes, highs, lows

def fmt_price(p):
    if p is None:
        return "-"
    if p >= 1000: return f"{p:,.1f}"
    if p >= 10: return f"{p:,.2f}"
    return f"{p:.3f}"

# ---------- 指標(自前) ----------
def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n

def sma_series(vals, n):
    return [sum(vals[i-n+1:i+1])/n if i >= n-1 else None for i in range(len(vals))]

def rsi(vals, n):
    if len(vals) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, n+1):
        d = vals[i] - vals[i-1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains/n, losses/n
    for i in range(n+1, len(vals)):
        d = vals[i] - vals[i-1]
        ag = (ag*(n-1) + max(d, 0)) / n
        al = (al*(n-1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    rs = ag/al
    return 100 - 100/(1+rs)

def stddev(vals):
    m = sum(vals)/len(vals)
    return math.sqrt(sum((v-m)**2 for v in vals)/len(vals))

def atr(highs, lows, closes, n):
    """ATR(平均真の値幅)。損切り幅の算出に使う。"""
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    if len(trs) < n:
        return None
    a = sum(trs[:n])/n
    for i in range(n, len(trs)):
        a = (a*(n-1) + trs[i])/n
    return a

# ---------- 各チェック ----------
def check_spike(cfg, state):
    sm = cfg["spike_monitor"]
    cd = state.setdefault("spike_cd", {})
    cooldown = sm["cooldown_minutes"]
    now = now_jst()
    for inst in sm["instruments"]:
        name, sym = inst["name"], inst["symbol"]
        try:
            closes, _, _ = yahoo_chart(sym, "1d", "1m")
        except Exception as e:
            print(f"[spike] {name}: {e}"); continue
        win = inst["window_minutes"]
        if len(closes) < win + 1:
            continue
        cur, past = closes[-1], closes[-(win+1)]
        if past == 0:
            continue
        pct = (cur - past) / past * 100
        if abs(pct) < inst["threshold_pct"]:
            continue
        last = cd.get(name)
        if last and (now - datetime.datetime.fromisoformat(last)).total_seconds()/60 < cooldown:
            continue
        arrow = "⬆ 上昇" if pct >= 0 else "⬇ 下落"
        tg_send(f"🚨 急変動 {name}\n{name} {arrow} {pct:+.2f}%（直近{win}分）\n現在値: {fmt_price(cur)}")
        cd[name] = now.isoformat()
        print(f"[spike] ALERT {name} {pct:+.2f}%")

def check_news(cfg, state):
    nm = cfg["news_monitor"]
    seen = state.setdefault("news_seen", [])
    first_run = (len(seen) == 0)
    items = []
    for feed in nm["feeds"]:
        try:
            raw = http_get(feed["url"]).decode("utf-8", "ignore")
        except Exception as e:
            print(f"[news] {feed['name']}: {e}"); continue
        for m in re.finditer(r"<item\b.*?</item>", raw, re.S | re.I):
            block = m.group(0)
            tm = re.search(r"<title>(.*?)</title>", block, re.S | re.I)
            lm = re.search(r"<link>(.*?)</link>", block, re.S | re.I)
            if not tm:
                continue
            title = re.sub(r"<!\[CDATA\[|\]\]>", "", tm.group(1)).strip()
            title = re.sub(r"<.*?>", "", title).strip()
            if not title:
                continue
            link = (lm.group(1).strip() if lm else "") or (feed["name"] + "|" + title)
            link = re.sub(r"<!\[CDATA\[|\]\]>", "", link).strip()
            items.append((link, title, feed["name"]))
    cur_ids = [it[0] for it in items]
    if first_run:
        state["news_seen"] = cur_ids[:500]
        print(f"[news] 初回シード {len(cur_ids)}件")
        return
    newitems = [it for it in items if it[0] not in seen]
    sent = 0
    for link, title, src in newitems:
        tl = title.lower()
        tags = []
        for r in nm["routing"]:
            if any(k.lower() in tl for k in r["keywords"]):
                tags.append(r["name"])
        if any(k.lower() in tl for k in nm["macro_keywords"]):
            tags.append(nm["macro_label"])
        tags = list(dict.fromkeys(tags))
        if not tags:
            continue
        if sent >= nm["max_per_cycle"]:
            break
        ja = translate(title, nm.get("translate_to_ja", True))
        body = ja if ja == title else f"{ja}\n(EN) {title}"
        tg_send(f"📰 [{' / '.join(tags)}] {src}\n{body}")
        sent += 1
        print(f"[news] sent: {title[:40]}")
    state["news_seen"] = (cur_ids + seen)[:500]

def check_calendar(cfg, state):
    cal = cfg["news_monitor"]["calendar"]
    lbl = cal["labels"]
    cst = state.setdefault("cal", {"last_digest": "", "sent": []})
    # 30分キャッシュ
    cache = cst.get("cache")
    fetched = cst.get("cache_at")
    raw = None
    if cache and fetched and (datetime.datetime.now(JST) - datetime.datetime.fromisoformat(fetched)).total_seconds() < 1800:
        raw = cache
    if raw is None:
        try:
            raw = json.loads(http_get(cal["feed_url"]).decode("utf-8"))
            cst["cache"] = raw
            cst["cache_at"] = now_jst().isoformat()
        except Exception as e:
            print(f"[cal] {e}"); return
    events = []
    for e in raw:
        if e.get("impact") not in cal["include_impacts"]:
            continue
        ok_cur = e.get("country") in cal["watched_currencies"]
        ok_kw = any(kw in (e.get("title") or "") for kw in cal["include_title_keywords"])
        if not (ok_cur or ok_kw):
            continue
        try:
            jst = datetime.datetime.fromisoformat(e["date"]).astimezone(JST)
        except Exception:
            continue
        events.append({"id": f"{e.get('country')}|{e.get('title')}|{e['date']}",
                       "jst": jst, "country": e.get("country"), "impact": e.get("impact"),
                       "title": e.get("title"), "f": e.get("forecast"), "p": e.get("previous")})
    events.sort(key=lambda x: x["jst"])
    now = now_jst()
    today = now.strftime("%Y-%m-%d")
    todays = [e for e in events if e["jst"].strftime("%Y-%m-%d") == today]
    # 日次ダイジェスト
    if cst["last_digest"] != today and now.hour >= cal["daily_digest_hour_jst"]:
        if todays:
            lines = []
            for e in todays:
                lines.append(f"{e['jst']:%H:%M} [{e['impact']}] {e['country']} {e['title']}")
            tg_send(f"📰 {lbl['digest_title']}\n" + "\n".join(lines))
        else:
            tg_send(f"📰 {lbl['digest_title']}\n{lbl['none_today']}")
        cst["last_digest"] = today
        print("[cal] digest sent")
    # 直前リマインダー
    for e in events:
        if e["id"] in cst["sent"]:
            continue
        mins = (e["jst"] - now).total_seconds()/60
        if 0 < mins <= cal["reminder_minutes"]:
            tg_send(f"📰 {lbl['reminder_title']}\n{e['jst']:%H:%M} [{e['impact']}] {e['country']} {e['title']}\n"
                    f"{lbl['until']} {mins:.0f}{lbl['minutes']}（{lbl['forecast']}:{e['f']} {lbl['previous']}:{e['p']}）")
            cst["sent"].append(e["id"])
            print(f"[cal] reminder: {e['title']}")
    cst["sent"] = cst["sent"][-200:]

def check_signals(cfg, state):
    s = cfg["signal_monitor"]
    cd = state.setdefault("signal_cd", {})
    cooldown = s["cooldown_minutes"]
    now = now_jst()
    sigset = set(s["signals"])
    for inst in cfg["spike_monitor"]["instruments"]:
        name, sym = inst["name"], inst["symbol"]
        try:
            closes, highs, lows = yahoo_chart(sym, s["range"], s["interval"])
        except Exception as e:
            print(f"[sig] {name}: {e}"); continue
        need = max(s["sma_long"], s["bb_period"], s["breakout_lookback"], s["rsi_period"]) + 2
        if len(closes) < need:
            continue
        cur = closes[-1]
        a = atr(highs, lows, closes, s.get("atr_period", 14))
        sigs = []
        if "sma" in sigset:
            ss = sma_series(closes, s["sma_short"]); sl = sma_series(closes, s["sma_long"])
            if ss[-2] and sl[-2] and ss[-1] and sl[-1]:
                if ss[-2] <= sl[-2] and ss[-1] > sl[-1]:
                    sigs.append(("buy", f"ゴールデンクロス(SMA{s['sma_short']}>SMA{s['sma_long']})"))
                elif ss[-2] >= sl[-2] and ss[-1] < sl[-1]:
                    sigs.append(("sell", f"デッドクロス(SMA{s['sma_short']}<SMA{s['sma_long']})"))
        if "rsi" in sigset:
            rv = rsi(closes, s["rsi_period"]); rp = rsi(closes[:-1], s["rsi_period"])
            if rv is not None and rp is not None:
                if rp >= s["rsi_low"] and rv < s["rsi_low"]:
                    sigs.append(("buy", f"RSI過売り({rv:.0f})"))
                elif rp <= s["rsi_high"] and rv > s["rsi_high"]:
                    sigs.append(("sell", f"RSI過買い({rv:.0f})"))
        if "breakout" in sigset:
            lb = s["breakout_lookback"]
            ph = max(highs[-(lb+1):-1]); pl = min(lows[-(lb+1):-1])
            if cur > ph: sigs.append(("buy", f"直近{lb}本高値ブレイク"))
            elif cur < pl: sigs.append(("sell", f"直近{lb}本安値ブレイク"))
        if "bb" in sigset:
            p = s["bb_period"]; window = closes[-p:]
            mid = sum(window)/p; sd = stddev(window)
            if cur <= mid - s["bb_std"]*sd: sigs.append(("buy", f"ボリンジャー下限({s['bb_std']}σ)"))
            elif cur >= mid + s["bb_std"]*sd: sigs.append(("sell", f"ボリンジャー上限({s['bb_std']}σ)"))
        for direction in ("buy", "sell"):
            reasons = [r for d, r in sigs if d == direction]
            if not reasons:
                continue
            key = f"{name}:{direction}"
            last = cd.get(key)
            if last and (now - datetime.datetime.fromisoformat(last)).total_seconds()/60 < cooldown:
                continue
            arrow = "🟢 買い" if direction == "buy" else "🔴 売り"
            strength = "★" * min(len(reasons), 3)
            risk_line = ""
            if a:
                mult = s.get("atr_stop_mult", 1.5)
                rr = s.get("rr_ratio", 1.5)
                if direction == "buy":
                    stop = cur - mult*a; tgt = cur + mult*a*rr
                else:
                    stop = cur + mult*a; tgt = cur - mult*a*rr
                rk = abs(cur-stop)/cur*100
                rw = abs(tgt-cur)/cur*100
                risk_line = (f"\n🛑 損切り目安: {fmt_price(stop)}（{rk:.2f}%）"
                             f"\n🎯 利確目安: {fmt_price(tgt)}（{rw:.2f}%）"
                             f"\n※必ず損切りを置く（塩漬け厳禁）")
            tg_send(f"📈 シグナル {name} {strength}\n{arrow}（{s['interval']}足）\n"
                    + "\n".join("・"+r for r in reasons) + f"\n現在値: {fmt_price(cur)}" + risk_line)
            cd[key] = now.isoformat()
            print(f"[sig] {name} {direction} <- {reasons}")

def run_one(fn, cfg, state):
    try:
        fn(cfg, state)
    except Exception as e:
        print(f"[{fn.__name__}] 例外: {e}")

def git_commit_push():
    """状態ファイルをリポジトリに保存(GitHub Actions上でのみ動作)。"""
    if not (os.environ.get("GITHUB_ACTIONS") or os.path.isdir(os.path.join(HERE, ".git"))):
        return  # ローカル(非リポジトリ)では何もしない
    try:
        subprocess.run(["git", "add", "state"], cwd=HERE, check=False,
                       capture_output=True, timeout=60)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=HERE,
                              check=False, timeout=60)
        if diff.returncode == 0:
            return  # 変更なし
        subprocess.run(["git", "-c", "user.name=trade-bot",
                        "-c", "user.email=trade-bot@users.noreply.github.com",
                        "commit", "-m", "update state [skip ci]"],
                       cwd=HERE, check=False, capture_output=True, timeout=60)
        subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=HERE,
                       check=False, capture_output=True, timeout=60)
        subprocess.run(["git", "push"], cwd=HERE, check=False,
                       capture_output=True, timeout=60)
    except Exception as e:
        print(f"[git] {e}")

def main():
    cfg = load_json(CONFIG, {})
    state = load_json(STATE, {})

    loop_min = float(os.environ.get("LOOP_MINUTES", "0") or "0")
    if loop_min <= 0:
        # 単発実行(手動テスト等)
        for fn in (check_spike, check_news, check_calendar, check_signals):
            run_one(fn, cfg, state)
        save_state(state)
        print("done (single)")
        return

    # ループ実行(GitHub Actionsの1起動で長時間回し続ける)
    poll = int(os.environ.get("POLL_SECONDS", "120"))
    commit_every = float(os.environ.get("GIT_COMMIT_MINUTES", "10")) * 60
    news_every = int(os.environ.get("NEWS_EVERY_CYCLES", "2"))     # 急変動より低頻度
    signal_every = int(os.environ.get("SIGNAL_EVERY_CYCLES", "6")) # 15分足なので低頻度
    end = time.time() + loop_min * 60
    last_commit = time.time()
    cycle = 0
    print(f"=== loop start: {loop_min:.0f}min, poll {poll}s ===")
    while time.time() < end:
        run_one(check_spike, cfg, state)                  # 毎回(急変動)
        if cycle % news_every == 0:
            run_one(check_news, cfg, state)
            run_one(check_calendar, cfg, state)
        if cycle % signal_every == 0:
            run_one(check_signals, cfg, state)
        save_state(state)
        if time.time() - last_commit >= commit_every:
            git_commit_push()
            last_commit = time.time()
        cycle += 1
        if time.time() < end:
            time.sleep(poll)
    git_commit_push()
    print("done (loop)")

if __name__ == "__main__":
    main()
