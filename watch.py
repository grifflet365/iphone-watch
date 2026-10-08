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
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}
KAITORIX_URL = "https://kaitorix.app/series/iphone-18"
APPLE_URL = "https://www.apple.com/jp/shop/pickup-message-recommendations"
APPLE_LOCATION = "104-0061"  # 銀座。近隣の都内5店+川崎が返る
KANTO_STATES = {"東京都", "神奈川県", "埼玉県", "千葉県"}
TARGET_CAPACITIES = ("256GB", "512GB", "1TB")  # 通知対象の容量(全色)
STATE_FILE = Path(__file__).with_name("state.json")  # cmd_price は state_price.json に差し替える(価格と在庫で書き込みが衝突しないように)
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
FAIL_NOTIFY_AFTER = 5  # 連続失敗がこの回数に達したら1回だけ知らせる
STORES = ("銀座", "丸の内", "表参道", "渋谷", "新宿", "川崎")
COLORS = ("ブラック", "シルバー", "バーガンディ", "グレイシャー")


def discord_request(method, url, **kwargs):
    for attempt in range(5):
        response = requests.request(method, url, timeout=30, **kwargs)
        if response.status_code != 429:
            return response
        time.sleep(float(response.json().get("retry_after", 2)))
    response.raise_for_status()
    return response


def update_stock_table(state, rows, apple=None, error=False):
    """モデル別の固定メッセージを編集。候補にない組み合わせは不明。"""
    timestamp = datetime.now(timezone(timedelta(hours=9))).strftime("%m/%d %H:%M:%S JST")
    by_name = {row["name"]: row["part"] for row in rows}
    ids = state.setdefault("stock_table_messages", {})
    base = urlsplit(WEBHOOK)
    for model in ("Pro", "Pro Max"):
        lines = ["容量 / 色 | 銀 丸 表 渋 新 川", "---------------------------"]
        for capacity in TARGET_CAPACITIES:
            for color in COLORS:
                part = by_name.get(f"{model} {capacity} {color}")
                available = apple.get(part, set()) if apple is not None else set()
                cells = " ".join("○" if store in available else "？" for store in STORES)
                lines.append(f"{capacity} {color} | {cells}")
        text = (f"🍎 **iPhone 18 {model} 店舗受け取り在庫**\n"
                f"更新: {timestamp}" + (" ⚠ 取得失敗" if error else "") + "\n"
                "銀=銀座 丸=丸の内 表=表参道 渋=渋谷 新=新宿 川=川崎\n"
                "○=受け取り可能　？=未確認・候補なし・取得失敗\n"
                "```text\n" + "\n".join(lines) + "\n```\n"
                "https://www.apple.com/jp/shop/buy-iphone/iphone-18-pro")
        if not WEBHOOK:
            print(text, flush=True)
            continue
        payload = {"content": text, "allowed_mentions": {"parse": []}}
        if ids.get(model):
            url = urlunsplit(base._replace(path=base.path.rstrip("/") + "/messages/" + ids[model]))
            response = discord_request("PATCH", url, json=payload)
            if response.status_code != 404:
                response.raise_for_status()
                continue
        response = discord_request("POST", WEBHOOK, params={"wait": "true"}, json=payload)
        response.raise_for_status()
        ids[model] = response.json()["id"]
        save_state(state)  # 初回作成直後にID保存。以降は同じメッセージを編集


def send(text):
    print(text, flush=True)
    if not WEBHOOK:
        return
    for i in range(0, len(text), 1900):
        r = discord_request("POST", WEBHOOK, json={"content": text[i:i + 1900]})
        r.raise_for_status()


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
    global STATE_FILE
    STATE_FILE = STATE_FILE.with_name("state_price.json")
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
    rows = state.get("stock_table_rows", [])
    update_stock_table(state, rows)
    while True:
        try:
            if n % 5 == 0 or n == 0:  # 価格表とAmazon在庫は5回に1回
                rows = fetch_prices()
                state["stock_table_rows"] = rows
            apple = fetch_apple_stock([r["part"] for r in rows])
            update_stock_table(state, rows, apple)
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
            print(f"取得失敗({fails}回連続): {type(e).__name__}", file=sys.stderr, flush=True)
            try:
                update_stock_table(state, rows, error=True)
            except requests.RequestException:
                print("在庫表の更新にも失敗しました", file=sys.stderr, flush=True)
            if fails >= FAIL_NOTIFY_AFTER and not notified_fail:
                send(f"⚠️ 在庫取得が{fails}回連続で失敗しています: {type(e).__name__}")
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
