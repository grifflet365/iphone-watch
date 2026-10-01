"""iPhone 18 Pro / Pro Max 価格・在庫ウォッチ

  python watch.py price                 価格一覧をDiscordに送る(1日3回想定)
  python watch.py stock --minutes 240   入荷を監視し、新たに買える状態になった瞬間に送る

- 価格: 買取X(kaitorix.app)の一覧表から 型番ごとの定価・買取最高値・差額 を取得
- Apple在庫: 公式サイトの店舗受け取り在庫(1都3県の直営店)を型番ごとに取得
- Amazon在庫: 買取Xの一覧に載っている Amazon本体の新品在庫の有無 を利用
- DISCORD_WEBHOOK_URL が未設定なら送信せず画面表示のみ(ドライラン)
"""
import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}
KAITORIX_URL = "https://kaitorix.app/series/iphone-18"
APPLE_URL = "https://www.apple.com/jp/shop/pickup-message-recommendations"
APPLE_LOCATION = "104-0061"  # 銀座。近隣の都内5店+川崎が返る
KANTO_STATES = {"東京都", "神奈川県", "埼玉県", "千葉県"}
TARGET_CAPACITIES = ("256GB", "512GB", "1TB")  # 通知対象の容量(全色)
STATE_FILE = Path(__file__).with_name("state.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
FAIL_NOTIFY_AFTER = 5  # 連続失敗がこの回数に達したら1回だけ知らせる


def send(text):
    print(text, flush=True)
    if not WEBHOOK:
        return
    for i in range(0, len(text), 1900):
        r = requests.post(WEBHOOK, json={"content": text[i:i + 1900]}, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            requests.post(WEBHOOK, json={"content": text[i:i + 1900]}, timeout=30)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def fetch_prices():
    t = requests.get(KAITORIX_URL, headers=UA, timeout=30)
    t.raise_for_status()
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", t.text, re.S):
        m = re.search(r'alt="(iPhone 18[^"]*?)".*?nmb-code">([A-Z0-9]{5}J/A)<', tr, re.S)
        if not m or "Duo" in m.group(1):
            continue
        name = html.unescape(m.group(1))
        if not any(f" {c} " in name for c in TARGET_CAPACITIES):
            continue
        msrp = re.search(r'data-label="定価">¥([\d,]+)', tr)
        best = re.search(r'nmb-best">¥([\d,]+)</b><span class="nmb-sub">([^<]*)', tr)
        amz = re.search(r'class="nmb-az ([^"]*)"', tr)
        num = lambda x: int(x.group(1).replace(",", "")) if x else None
        rows.append({
            "name": name.replace("iPhone 18 ", ""),
            "part": m.group(2),
            "msrp": num(msrp),
            "best": num(best),
            "shop": best.group(2) if best else "",
            "amazon": ("oos" not in amz.group(1)) if amz else None,
        })
    if not rows:
        raise RuntimeError("買取Xの表から型番を1件も読み取れませんでした(ページ構造が変わった可能性)")
    return rows


def fetch_apple_stock(parts):
    """{型番: [店舗名,...]} 受け取り可能な1都3県の直営店"""
    stock = {p: set() for p in parts}
    for p in parts:
        r = requests.get(
            APPLE_URL, headers={**UA, "x-requested-with": "XMLHttpRequest"}, timeout=30,
            params={"fae": "true", "mts.0": "regular", "mts.1": "compact",
                    "searchNearby": "true", "product": p, "location": APPLE_LOCATION})
        r.raise_for_status()
        for s in r.json()["body"]["PickupMessage"]["stores"]:
            if s["state"] not in KANTO_STATES:
                continue
            for part, info in s["partsAvailability"].items():
                if part in stock and info.get("storePickEligible"):
                    stock[part].add(s["storeName"])
        time.sleep(0.3)
    return stock


def diff_text(r):
    if r["best"] is None or r["msrp"] is None:
        return "差額不明"
    return f"差額{r['best'] - r['msrp']:+,}円(買取{r['best']:,}・{r['shop']})"


def cmd_price(_args):
    state = load_state()
    rows = fetch_prices()
    prev = state.get("best", {})
    rows.sort(key=lambda r: -(r["best"] - r["msrp"]) if r["best"] and r["msrp"] else 1e9)
    lines = [f"📱 **iPhone 18 Pro / Pro Max 買取価格** ({time.strftime('%m/%d %H:%M')})"]
    for r in rows:
        chg = ""
        if r["part"] in prev and r["best"] is not None and prev[r["part"]] != r["best"]:
            chg = f" 〔前回比 {r['best'] - prev[r['part']]:+,}〕"
        mark = "🟢" if r["best"] and r["msrp"] and r["best"] > r["msrp"] else "▫️"
        lines.append(f"{mark} {r['name']}: 定価{r['msrp']:,} {diff_text(r)}{chg}")
    send("\n".join(lines))
    state["best"] = {r["part"]: r["best"] for r in rows if r["best"] is not None}
    save_state(state)


def cmd_stock(args):
    state = load_state()
    seen = {k: set(v) for k, v in state.get("apple", {}).items()}
    amz_seen = state.get("amazon", {})
    end = time.time() + args.minutes * 60
    fails, notified_fail, n = 0, False, 0
    while True:
        try:
            if n % 5 == 0 or n == 0:  # 価格表とAmazon在庫は5回に1回
                rows = fetch_prices()
            apple = fetch_apple_stock([r["part"] for r in rows])
            fails, notified_fail = 0, False
            first_run = not seen
            for r in rows:
                new_stores = apple[r["part"]] - seen.get(r["part"], set())
                if new_stores and not first_run:
                    send(f"🍎 **Apple入荷** {r['name']}\n受取: {'、'.join(sorted(new_stores))}\n{diff_text(r)}\n"
                         f"https://www.apple.com/jp/shop/buy-iphone/iphone-18-pro")
                seen[r["part"]] = apple[r["part"]]
                if n % 5 == 0 and r["amazon"] is not None:
                    if r["amazon"] and not amz_seen.get(r["part"]) and amz_seen:
                        send(f"📦 **Amazon入荷** {r['name']}\n{diff_text(r)}")
                    amz_seen[r["part"]] = r["amazon"]
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"取得失敗({fails}回連続): {e}", file=sys.stderr, flush=True)
            if fails >= FAIL_NOTIFY_AFTER and not notified_fail:
                send(f"⚠️ 在庫取得が{fails}回連続で失敗しています: {e}")
                notified_fail = True
        n += 1
        if time.time() + args.interval >= end:
            break
        time.sleep(args.interval)
    state["apple"] = {k: sorted(v) for k, v in seen.items()}
    state["amazon"] = amz_seen
    save_state(state)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("price").set_defaults(fn=cmd_price)
    st = sub.add_parser("stock")
    st.add_argument("--minutes", type=int, default=1, help="この時間だけ監視を続ける(分)")
    st.add_argument("--interval", type=int, default=60, help="チェック間隔(秒)")
    st.set_defaults(fn=cmd_stock)
    a = ap.parse_args()
    a.fn(a)
