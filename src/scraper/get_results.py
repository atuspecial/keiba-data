#!/usr/bin/env python3
"""
レース結果スクレイパー（学習データ用）

使い方:
    python src/scraper/get_results.py --date 20260524
    python src/scraper/get_results.py  # 今日の日付

出力: data/results/results_YYYYMMDD.csv
カラム: date, venue, race_no, race_name, detail, finish_pos,
        horse_no, horse_name, final_odds, popularity, finish_time, race_id
"""

import argparse
import csv
import re
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
DELAY = 1.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
    "Referer": "https://race.netkeiba.com/",
}


def get_venues(date_str: str) -> list[dict]:
    """開催場所とレースIDリストを取得する（get_odds.py と同様）"""
    url = "https://race.netkeiba.com/top/race_list_get_date_list.html"
    try:
        resp = requests.get(url, headers=HEADERS, params={"kaisai_date": date_str}, timeout=15)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "lxml")
        tag = soup.find("li", {"date": date_str})
        group_id = tag["group"] if tag else None
    except Exception:
        group_id = None

    if group_id:
        url2 = "https://race.netkeiba.com/top/race_list_sub.html"
        params = {"kaisai_date": date_str, "current_group": group_id}
    else:
        url2 = f"https://race.netkeiba.com/top/race_list.html?kaisai_date={date_str}"
        params = {}

    try:
        resp = requests.get(url2, headers=HEADERS, params=params, timeout=15)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        print(f"[ERROR] 開催情報取得失敗: {e}")
        return []

    venues = []
    for venue_block in soup.select("div.RaceList_Box"):
        name_tag = venue_block.select_one(".RaceList_DataItem.RaceList_DataItem--Venue a, .RaceList_ItemTitle a")
        if not name_tag:
            continue
        venue_name = name_tag.get_text(strip=True)
        race_links = venue_block.select("a[href*='race_id=']")
        race_ids = []
        for a in race_links:
            m = re.search(r"race_id=(\d+)", a.get("href", ""))
            if m:
                race_ids.append(m.group(1))
        if race_ids:
            venues.append({"name": venue_name, "race_ids": list(dict.fromkeys(race_ids))})
    return venues


def scrape_result(race_id: str) -> list[dict]:
    """
    result.html から着順・タイム・最終オッズを取得する。
    レースがまだ終了していない場合は空リストを返す。
    """
    url = f"https://race.netkeiba.com/race/result.html?race_id={race_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        raw = resp.content.decode("euc-jp", errors="replace")
        soup = BeautifulSoup(raw, "lxml")
    except Exception as e:
        print(f"  [ERROR] {race_id}: {e}")
        return []

    # 着順テーブルの行を取得
    rows = soup.select("table.RaceTable01 tr.HorseList")
    if not rows:
        return []

    # レース名・詳細
    race_name_tag = soup.select_one("div.RaceName, h1.RaceName")
    race_name = race_name_tag.get_text(strip=True) if race_name_tag else ""
    detail_tag = soup.select_one("div.RaceData01, .Race_Data")
    detail = re.sub(r"\s+", " ", detail_tag.get_text(" ", strip=True)).strip() if detail_tag else ""

    results = []
    for tr in rows:
        try:
            pos_tag = tr.select_one("div.Rank")
            if not pos_tag:
                continue
            pos_text = re.sub(r"\D", "", pos_tag.get_text(strip=True))
            if not pos_text:
                continue
            finish_pos = int(pos_text)

            # 馬番
            umaban_td = tr.select_one("td.Num.Txt_C div, td.Num div")
            horse_no = int(re.sub(r"\D", "", umaban_td.get_text(strip=True))) if umaban_td else 0

            # 馬名
            name_tag = tr.select_one("span.HorseNameSpan, span.Horse_Name")
            horse_name = name_tag.get_text(strip=True) if name_tag else ""

            # タイム
            time_tags = tr.select("span.RaceTime")
            finish_time = time_tags[0].get_text(strip=True) if time_tags else ""

            # 人気
            popularity_tag = tr.select_one("span.OddsPeople")
            popularity = int(re.sub(r"\D", "", popularity_tag.get_text(strip=True))) if popularity_tag else 0

            # 最終単勝オッズ
            odds_tag = tr.select_one("span.Odds_Ninki")
            raw_odds = odds_tag.get_text(strip=True).replace(",", "") if odds_tag else ""
            final_odds = float(raw_odds) if raw_odds and re.match(r"[\d.]+", raw_odds) else 0.0

            results.append({
                "finish_pos": finish_pos,
                "horse_no": horse_no,
                "horse_name": horse_name,
                "final_odds": final_odds,
                "popularity": popularity,
                "finish_time": finish_time,
                "race_name": race_name,
                "detail": detail,
                "race_id": race_id,
            })
        except (ValueError, AttributeError):
            continue

    return sorted(results, key=lambda x: x["finish_pos"])


def main() -> None:
    parser = argparse.ArgumentParser(description="レース結果スクレイパー")
    parser.add_argument("--date", help="日付 YYYYMMDD（省略時: 今日）")
    args = parser.parse_args()

    date_str = args.date or datetime.now(JST).strftime("%Y%m%d")
    display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    print(f"レース結果取得: {display_date}")
    venues = get_venues(date_str)
    if not venues:
        print("開催情報なし。終了します。")
        return

    print(f"開催場所: {', '.join(v['name'] for v in venues)}")

    out_dir = Path("data/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"results_{date_str}.csv"

    fieldnames = [
        "date", "venue", "race_no", "race_name", "detail",
        "finish_pos", "horse_no", "horse_name",
        "final_odds", "popularity", "finish_time", "race_id",
    ]

    total = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for venue in venues:
            venue_name = venue["name"]
            for i, race_id in enumerate(venue["race_ids"], 1):
                print(f"  {venue_name} {i}R ({race_id}) ...", end=" ")
                rows = scrape_result(race_id)
                if not rows:
                    print("未終了またはデータなし")
                    time.sleep(DELAY)
                    continue
                for row in rows:
                    writer.writerow({
                        "date": display_date,
                        "venue": venue_name,
                        "race_no": i,
                        **{k: row[k] for k in fieldnames[3:]},
                    })
                print(f"{len(rows)}頭")
                total += len(rows)
                time.sleep(DELAY)

    if total > 0:
        print(f"\n結果保存完了: {out_path} ({total}件)")
    else:
        out_path.unlink(missing_ok=True)
        print("\n終了済みレースなし。ファイル未作成。")


if __name__ == "__main__":
    main()
