"""自分のPCで動かす入荷アラート(iPhone Duo 全8モデル)

  python local_alert.py [--interval 5]

1都3県のApple Storeで受け取れる状態になった瞬間に、音を鳴らして
そのモデルの購入ページ(容量・色が選ばれた状態)をブラウザで開きます。
店舗の選択と支払いはご自身で操作します(購入の自動化はしません)。
止めるときは Ctrl+C。連続で取得に失敗したら間隔を自動で伸ばします。
"""
import argparse
import sys
import time
import webbrowser

try:
    import winsound
except ImportError:  # Windows以外
    winsound = None

import watch

MODELS = {
    "MK244J/A": "Duo 256GB スターホワイト",
    "MK254J/A": "Duo 256GB ナイトスカイ",
    "MK264J/A": "Duo 512GB スターホワイト",
    "MK274J/A": "Duo 512GB ナイトスカイ",
    "MK284J/A": "Duo 1TB スターホワイト",
    "MK294J/A": "Duo 1TB ナイトスカイ",
    "MK2A4J/A": "Duo 2TB スターホワイト",
    "MK2C4J/A": "Duo 2TB ナイトスカイ",
}
MAX_INTERVAL = 60


def beep():
    if winsound:
        for freq in (1000, 1500, 1000, 1500):
            winsound.Beep(freq, 200)
    else:
        print("\a", end="", flush=True)


def now():
    return time.strftime("%H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=5, help="チェック間隔(秒)")
    args = ap.parse_args()
    seen = None  # 前回の {型番: {店舗}}。None=初回(通知しない)
    interval, fails, last_beat = args.interval, 0, 0
    print(f"[{now()}] 監視開始: {len(MODELS)}モデル / {args.interval:g}秒間隔 (Ctrl+Cで停止)", flush=True)
    while True:
        try:
            stock = watch.fetch_apple_stock(list(MODELS))
            if fails:
                print(f"[{now()}] 取得が回復しました", flush=True)
            fails, interval = 0, args.interval
            if seen is not None:
                for part, stores in stock.items():
                    new = stores - seen[part]
                    if new:
                        print(f"[{now()}] ★入荷 {MODELS[part]}: {'、'.join(sorted(new))}", flush=True)
                        webbrowser.open(f"https://www.apple.com/jp/shop/product/{part}")
                        beep()
                    gone = seen[part] - stores
                    if gone:
                        print(f"[{now()}] 売り切れ {MODELS[part]}: {'、'.join(sorted(gone))}", flush=True)
            seen = stock
            if time.time() - last_beat >= 60:  # 動いていることが分かるように1分ごとに表示
                have = [f"{MODELS[p]}({'、'.join(sorted(s))})" for p, s in stock.items() if s]
                print(f"[{now()}] 監視中 在庫あり: {', '.join(have) if have else 'なし'}", flush=True)
                last_beat = time.time()
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            fails += 1
            interval = min(MAX_INTERVAL, args.interval * (2 ** min(fails, 5)))
            print(f"[{now()}] 取得失敗({fails}回連続): {type(e).__name__} → {interval:g}秒後に再試行", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except KeyboardInterrupt:
        print("\n停止しました")
