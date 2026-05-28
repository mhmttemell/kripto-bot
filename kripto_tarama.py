"""
ULUBEY KRİPTO TARAMA BOTU  v2
Binance API → Gösterge Hesaplama → Telegram Bildirim
GitHub Actions ile 10 dakikada bir otomatik çalışır.

v2 düzeltmeleri:
  - Tüm tickerlar tek API çağrısıyla alınıyor (timeout sorunu çözüldü)
  - Sadece hacmi yüksek ilk 80 coin taranıyor (~3 dk, limit dahilinde)
  - Telegram 4096 karakter limiti için mesaj bölme eklendi
  - sleep süresi 0.05 → 0.02 indirildi
"""

import os, requests, time, math
from datetime import datetime

# ── Telegram ayarları (GitHub Secrets'tan okunur) ─────────────────────────────
TG_TOKEN  = os.environ.get("TG_TOKEN", "")
TG_CHATID = os.environ.get("TG_CHATID", "")

# ── Binance API listesi ────────────────────────────────────────────────────────
API_BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
]

INTERVAL     = "15m"
LIMIT        = 96       # 96 × 15dk = 24 saat veri
HEADERS      = {"User-Agent": "Mozilla/5.0"}
TIMEOUT      = 10       # saniye — 15'ten 10'a indirildi
MAX_COINS    = 80       # en yüksek hacimli 80 coin taranır (~3 dk)
TG_MAX_CHARS = 4000     # Telegram 4096 limit, güvenli marj bırakıldı

# ── Filtre eşikleri ────────────────────────────────────────────────────────────
MIN_CONFIDENCE = 45
MIN_VOLUME_X   = 1.5
MIN_GAIN_PCT   = 1.5
MIN_RR         = 1.0

# ── Yardımcı: USD/TL kuru ─────────────────────────────────────────────────────
def get_usdtl():
    for base in API_BASES:
        try:
            r = requests.get(f"{base}/api/v3/ticker/price?symbol=USDTTRY",
                             headers=HEADERS, timeout=TIMEOUT)
            return float(r.json()["price"])
        except:
            pass
    return 38.50

# ── DÜZELTME: Tüm 24s tickerları tek seferde al, hacme göre sırala ─────────────
def get_top_coins_by_volume(max_coins=MAX_COINS):
    """
    Tek API çağrısıyla tüm USDT tickerları al.
    Quoty hacmine göre büyükten küçüğe sıralayıp ilk max_coins coini döndür.
    Bu sayede klines çekilecek coin sayısı sınırlanır → timeout engellenir.
    """
    for base in API_BASES:
        try:
            r = requests.get(f"{base}/api/v3/ticker/24hr",
                             headers=HEADERS, timeout=20)
            tickers = r.json()
            usdt = []
            for t in tickers:
                sym = t.get("symbol", "")
                if (sym.endswith("USDT")
                        and "UP" not in sym and "DOWN" not in sym
                        and "BULL" not in sym and "BEAR" not in sym):
                    try:
                        usdt.append({
                            "sym":    sym,
                            "pct24h": float(t.get("priceChangePercent", 0)),
                            "vol24h": float(t.get("quoteVolume", 0)),
                        })
                    except:
                        pass
            # Hacme göre büyükten küçüğe sırala
            usdt.sort(key=lambda x: x["vol24h"], reverse=True)
            return usdt[:max_coins]
        except:
            pass
    return []

# ── Yardımcı: Binance klines (OHLCV) ─────────────────────────────────────────
def fetch_klines(sym):
    for base in API_BASES:
        try:
            url = f"{base}/api/v3/klines?symbol={sym}&interval={INTERVAL}&limit={LIMIT}"
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            data = r.json()
            if not isinstance(data, list) or len(data) < 40:
                continue
            ts = [d[0] for d in data]
            op = [float(d[1]) for d in data]
            hi = [float(d[2]) for d in data]
            lo = [float(d[3]) for d in data]
            cl = [float(d[4]) for d in data]
            vo = [float(d[5]) for d in data]
            return ts, op, hi, lo, cl, vo
        except:
            pass
    return None

# ── Gösterge: EMA ─────────────────────────────────────────────────────────────
def calc_ema(prices, period):
    if len(prices) < period:
        return [prices[0]] * len(prices)
    k = 2.0 / (period + 1)
    ema = [prices[0]]
    for p in prices[1:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema

# ── Gösterge: MACD (12,26,9) ─────────────────────────────────────────────────
def calc_macd(prices):
    e12 = calc_ema(prices, 12)
    e26 = calc_ema(prices, 26)
    macd = [a - b for a, b in zip(e12, e26)]
    signal = calc_ema(macd, 9)
    return macd, signal

# ── Gösterge: RSI (14) ────────────────────────────────────────────────────────
def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return [50.0] * len(prices)
    diffs = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [max(d, 0) for d in diffs]
    losses = [max(-d, 0) for d in diffs]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    rsi = [50.0] * (period + 1)
    for i in range(period, len(diffs)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = avg_g / (avg_l + 1e-10)
        rsi.append(100 - 100 / (1 + rs))
    return rsi

# ── Gösterge: Bollinger Bands (20, 2σ) ───────────────────────────────────────
def calc_bb(prices, period=20):
    bb_u, bb_m, bb_l = [], [], []
    for i in range(len(prices)):
        sl = prices[max(0, i - period + 1):i + 1]
        m = sum(sl) / len(sl)
        std = math.sqrt(sum((x - m)**2 for x in sl) / len(sl))
        bb_m.append(m)
        bb_u.append(m + 2 * std)
        bb_l.append(m - 2 * std)
    return bb_u, bb_m, bb_l

# ── Gösterge: ATR (14) ────────────────────────────────────────────────────────
def calc_atr(hi, lo, cl, period=14):
    tr = [hi[0] - lo[0]]
    for i in range(1, len(cl)):
        tr.append(max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1])))
    atr = [tr[0]]
    k = 1.0 / period
    for i in range(1, len(tr)):
        atr.append(tr[i] * k + atr[-1] * (1 - k))
    return atr

# ── Sinyal & Güven skoru ──────────────────────────────────────────────────────
def get_signal(rsi, vol_x, macd_val, macd_sig, e9, e21, e50, bb_pos, elevated):
    macd_buy = macd_val > macd_sig
    ema_up   = e9 > e21 and e21 > e50
    ema_b    = e9 > e21
    vol_b    = vol_x > 1.20
    rsi_ok   = 35 < rsi < 68
    bb_b     = bb_pos < 0.30
    surge    = vol_x >= 2.0

    sc = 0
    if macd_buy: sc += 22
    if ema_b:    sc += 12
    if ema_up:   sc += 10
    if vol_b:    sc += 8
    if rsi_ok:   sc += 8
    if bb_b:     sc += 6
    if surge:    sc += 10
    if elevated: sc = max(sc - 20, 0)

    ema_status  = "YUKARI" if ema_up else ("ASAGI" if e9 < e21 else "NOTR")
    macd_status = "AL" if macd_buy else "SAT"
    return sc, ema_status, macd_status

# ── DÜZELTME: Telegram mesajını 4096 karakter limitine böl ───────────────────
def send_telegram(msg):
    if not TG_TOKEN or not TG_CHATID:
        print("Telegram token/chatid eksik!")
        return False
    # Mesajı gerekirse parçalara böl
    chunks = []
    while len(msg) > TG_MAX_CHARS:
        cut = msg.rfind("\n", 0, TG_MAX_CHARS)
        if cut == -1:
            cut = TG_MAX_CHARS
        chunks.append(msg[:cut])
        msg = msg[cut:].lstrip("\n")
    chunks.append(msg)

    ok = True
    for chunk in chunks:
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            r = requests.post(url, data={
                "chat_id": TG_CHATID,
                "text": chunk,
                "parse_mode": "HTML"
            }, timeout=15)
            if r.status_code != 200:
                print(f"Telegram hata kodu: {r.status_code} — {r.text[:200]}")
                ok = False
        except Exception as e:
            print(f"Telegram hata: {e}")
            ok = False
    return ok

# ── Hacim etiketi ─────────────────────────────────────────────────────────────
def vol_label(vx):
    if vx >= 5:  return f"{vx:.1f}x (5x PATLAMA)"
    if vx >= 2:  return f"{vx:.1f}x (2x PATLAMA)"
    return f"{vx:.1f}x (1.5x+)"

# ── Ana tarama ────────────────────────────────────────────────────────────────
def main():
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    print(f"[{now_str}] Tarama başlıyor...")

    usdtl = get_usdtl()
    print(f"USD/TL: {usdtl:.2f}")

    # Tek API çağrısıyla en yüksek hacimli MAX_COINS coini al
    top_coins = get_top_coins_by_volume(MAX_COINS)
    if not top_coins:
        print("Coin listesi alınamadı!")
        return

    print(f"En yüksek hacimli {len(top_coins)} coin taranıyor...")

    match_coins  = []
    strong_coins = []
    ok_n = 0

    for coin_info in top_coins:
        sym    = coin_info["sym"]
        pct24h = coin_info["pct24h"]
        vol24h = coin_info["vol24h"]

        try:
            data = fetch_klines(sym)
            if not data:
                continue
            ts, op, hi, lo, cl, vo = data

            time.sleep(0.02)  # DÜZELTME: 0.05 → 0.02

            # Göstergeler
            rsi_arr         = calc_rsi(cl)
            macd_arr, sig_arr = calc_macd(cl)
            e9_arr          = calc_ema(cl, 9)
            e21_arr         = calc_ema(cl, 21)
            e50_arr         = calc_ema(cl, 50)
            bb_u, bb_m, bb_l = calc_bb(cl)
            atr_arr         = calc_atr(hi, lo, cl)

            cur_p   = cl[-1]
            cur_rsi = rsi_arr[-1]
            cur_atr = atr_arr[-1]
            atr_pct = cur_atr / max(cur_p, 1e-10) * 100

            # Hacim çarpanı (son mum / 20 mum ortalaması)
            mean_vol = sum(vo[-21:-1]) / 20
            vol_x    = vo[-1] / max(mean_vol, 1e-10)
            if not math.isfinite(vol_x) or vol_x <= 0:
                vol_x = 1.0

            # Bollinger pozisyonu
            bb_range = bb_u[-1] - bb_l[-1]
            bb_pos   = (cur_p - bb_l[-1]) / max(bb_range, 1e-10)

            # Alış / satış seviyeleri
            buy_p  = bb_l[-1] * 1.003
            sell_p = bb_u[-1] * 0.997
            if buy_p <= 0:       buy_p  = cur_p * 0.97
            if sell_p <= buy_p:  sell_p = cur_p * 1.06

            elevated = cur_p > buy_p * 1.015

            # Sinyal skoru (elevated dahil)
            sc, ema_st, macd_st = get_signal(
                cur_rsi, vol_x, macd_arr[-1], sig_arr[-1],
                e9_arr[-1], e21_arr[-1], e50_arr[-1], bb_pos,
                elevated=elevated
            )

            gain_pct = (sell_p - buy_p) / max(buy_p, 1e-10) * 100
            sl_pct   = max(2.5, atr_pct * 2)
            sl_price = buy_p * (1 - sl_pct / 100)
            rr       = gain_pct / max(sl_pct, 0.01)

            # ── KOŞUL ALARMI ──────────────────────────────────────────────────
            if (ema_st == "YUKARI" and macd_st == "AL"
                    and vol_x >= MIN_VOLUME_X
                    and sc > MIN_CONFIDENCE
                    and not elevated
                    and gain_pct >= MIN_GAIN_PCT
                    and rr >= MIN_RR):
                match_coins.append({
                    "sym": sym, "cur_p": cur_p, "buy_p": buy_p,
                    "sell_p": sell_p, "sl": sl_price, "rsi": cur_rsi,
                    "vol_x": vol_x, "gain_pct": gain_pct, "rr": rr,
                    "conf": sc, "pct24h": pct24h,
                    "ema_st": ema_st, "macd_st": macd_st,
                })

            # ── ANA RAPOR: güçlü sinyal ───────────────────────────────────────
            if sc >= 70 and vol_x >= 2.0 and ema_st == "YUKARI" and not elevated:
                strong_coins.append({
                    "sym": sym, "cur_p": cur_p, "buy_p": buy_p,
                    "sell_p": sell_p, "sl": sl_price, "rsi": cur_rsi,
                    "vol_x": vol_x, "gain_pct": gain_pct, "rr": rr,
                    "conf": sc, "pct24h": pct24h,
                })

            ok_n += 1

        except Exception as e:
            print(f"{sym} hata: {e}")

    print(f"Tamamlandı: {ok_n}/{len(top_coins)} coin")

    # ── ANA RAPOR ─────────────────────────────────────────────────────────────
    lines = [
        f"<b>ULUBEY KRİPTO — {now_str}</b>",
        f"Tarama: hacimce ilk {len(top_coins)} coin  |  USD/TL: {usdtl:.2f}",
        "",
    ]
    if strong_coins:
        lines.append(f"<b>GÜÇLÜ AL SİNYALLERİ ({len(strong_coins)} coin)</b>")
        for i, d in enumerate(strong_coins[:5], 1):
            cs = d["sym"].replace("USDT", "")
            lines += [
                f"\n<b>{i}. {cs}/USDT</b>  —  Güven: <b>{d['conf']:.0f}%</b>",
                f"Fiyat: <b>{d['cur_p']*usdtl:.4f} TL</b>  |  24h: {d['pct24h']:.1f}%",
                f"Hacim: {vol_label(d['vol_x'])}  |  RSI: {d['rsi']:.1f}",
                f"Alış: <b>{d['buy_p']*usdtl:.4f} TL</b>  →  Hedef: <b>{d['sell_p']*usdtl:.4f} TL</b>  (+{d['gain_pct']:.1f}%)",
                f"Stop: {d['sl']*usdtl:.4f} TL  |  R/R: 1:{d['rr']:.1f}",
                "------",
            ]
    else:
        lines.append("<b>Güçlü AL sinyali bulunamadı</b> — piyasa sakin, bekleyin.")

    send_telegram("\n".join(lines))
    print(f"Ana rapor gönderildi ({len(strong_coins)} güçlü sinyal)")

    # ── KOŞUL ALARMI ──────────────────────────────────────────────────────────
    if match_coins:
        lines2 = [
            f"<b>ULUBEY — KOŞUL ALARMI  {datetime.now().strftime('%H:%M')}</b>",
            f"<b>{len(match_coins)} coin 4 koşulu birden karşılıyor:</b>",
            "✅ EMA = YUKARI   ✅ MACD = AL",
            "✅ Hacim > 1.5x   ✅ Güven > 65%",
            "",
        ]
        for i, d in enumerate(match_coins[:8], 1):
            cs = d["sym"].replace("USDT", "")
            lines2 += [
                f"<b>{i}. {cs}/USDT</b>  —  Güven: <b>{d['conf']:.0f}%</b>  |  R/R: 1:{d['rr']:.1f}",
                f"Fiyat: <b>{d['cur_p']*usdtl:.4f} TL</b>  |  24h: {d['pct24h']:.1f}%",
                f"✅ EMA: {d['ema_st']}  |  ✅ MACD: {d['macd_st']}",
                f"✅ Hacim: {vol_label(d['vol_x'])}  |  RSI: {d['rsi']:.1f}",
                f"Alış: <b>{d['buy_p']*usdtl:.4f} TL</b>  →  Hedef: <b>{d['sell_p']*usdtl:.4f} TL</b>  (+{d['gain_pct']:.1f}%)",
                f"Stop: {d['sl']*usdtl:.4f} TL  |  R/R: 1:{d['rr']:.1f}",
                "- - -",
            ]
        lines2.append(f"\nTarama: ilk {len(top_coins)} coin  |  USD/TL: {usdtl:.2f}")
        send_telegram("\n".join(lines2))
        print(f"Koşul alarmı gönderildi ({len(match_coins)} coin)")
    else:
        print("Koşul alarmı: eşleşen coin yok, mesaj gönderilmedi.")

if __name__ == "__main__":
    main()
