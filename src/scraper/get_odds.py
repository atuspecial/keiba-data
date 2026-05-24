#!/usr/bin/env python3
"""
keiba-get: インタラクティブ オッズ取得スクリプト

使い方:
    python src/scraper/get_odds.py
    python src/scraper/get_odds.py --date 20260524
"""

import argparse
import csv
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table
from rich import box

JST = timezone(timedelta(hours=9))


def now_jst() -> str:
    """現在の JST 日時を "YYYY-MM-DD HH:MM:SS+09:00" 形式で返す"""
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S+09:00")

console = Console()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
    "Referer": "https://race.netkeiba.com/",
}
DELAY = 1.5  # リクエスト間隔（秒）


# ── Step 1: 日付選択 ──────────────────────────────────────

def select_date(preset: str | None = None) -> str:
    """YYYYMMDD 形式の日付文字列を返す"""
    if preset:
        return preset.replace("-", "")

    today = date.today()
    tomorrow = today + timedelta(days=1)

    console.rule("[bold cyan]keiba-get  ─  日付選択[/bold cyan]")
    console.print(f"  [bold]1[/bold]. 今日    [cyan]{today.strftime('%Y-%m-%d (%a)')}[/cyan]")
    console.print(f"  [bold]2[/bold]. 明日    [cyan]{tomorrow.strftime('%Y-%m-%d (%a)')}[/cyan]")
    console.print("  [bold]3[/bold]. 直接入力 [dim](YYYY-MM-DD)[/dim]")

    choice = input("\n番号を入力 [1]: ").strip() or "1"

    if choice == "2":
        return tomorrow.strftime("%Y%m%d")
    elif choice == "3":
        raw = input("日付を入力 (YYYY-MM-DD): ").strip()
        return raw.replace("-", "")
    return today.strftime("%Y%m%d")


# ── Step 2: 開催競馬場取得・選択 ──────────────────────────

def _get_group_id(date_str: str) -> str:
    """
    race_list_get_date_list.html から current_group を取得する。
    race_list_sub.html の呼び出しに必要なパラメータ。
    """
    url = "https://race.netkeiba.com/top/race_list_get_date_list.html"
    try:
        resp = requests.get(url, headers=HEADERS, params={"kaisai_date": date_str}, timeout=15)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "lxml")
        # <li date="YYYYMMDD" group="10YYYYMMDD"> を探す
        li = soup.find("li", {"date": date_str})
        if li:
            return li.get("group", "")
        # 見つからない場合は先頭グループを返す
        li_any = soup.find("li", attrs={"group": True})
        return li_any.get("group", "") if li_any else ""
    except Exception:
        return ""


def get_venues(date_str: str) -> list[dict]:
    """その日開催の競馬場リストを取得する"""
    group_id = _get_group_id(date_str)
    if group_id:
        url = "https://race.netkeiba.com/top/race_list_sub.html"
        params: dict = {"kaisai_date": date_str, "current_group": group_id}
    else:
        # フォールバック: 旧URL（JS動的ロードのためデータが取れない可能性あり）
        url = f"https://race.netkeiba.com/top/race_list.html?kaisai_date={date_str}"
        params = {}
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        console.print(f"[red]取得失敗: {e}[/red]")
        return []

    venues = []
    for block in soup.select("dl.RaceList_DataList"):
        venue_tag = block.select_one("dt")
        race_links = block.select("dd a")
        if not venue_tag or not race_links:
            continue
        # RaceList_DataTitle から競馬場名のみを抽出（天気情報を除外）
        title_tag = venue_tag.select_one("p.RaceList_DataTitle")
        if title_tag:
            # <small>タグ（回・日目）を除いた中心テキストのみ取得
            venue_name = "".join(
                t for t in title_tag.find_all(string=True, recursive=False)
            ).strip()
            if not venue_name:
                venue_name = title_tag.get_text(strip=True)
        else:
            venue_name = venue_tag.get_text(strip=True)
        # race_id を重複なく収集（各レースにリンクが複数ある場合があるため）
        seen_ids: set[str] = set()
        races = []
        for a in race_links:
            href = a.get("href", "")
            m = re.search(r"race_id=(\d+)", href)
            if not m:
                continue
            race_id = m.group(1)
            if race_id in seen_ids:
                continue
            seen_ids.add(race_id)
            # race_id 末尾2桁がレース番号
            try:
                race_no = int(race_id[-2:])
            except (ValueError, IndexError):
                race_no = len(races) + 1
            races.append({"race_no": race_no, "race_id": race_id})
        venues.append({"name": venue_name, "races": races})
    return venues


def select_venue(venues: list[dict]) -> dict | None:
    if not venues:
        console.print("[yellow]開催情報が取得できませんでした。日付を変えてお試しください。[/yellow]")
        return None

    console.rule("[bold cyan]開催競馬場[/bold cyan]")
    for i, v in enumerate(venues, 1):
        console.print(f"  [bold]{i}[/bold]. {v['name']:6s}  ({len(v['races'])}R)")

    choice = input("\n番号を入力: ").strip()
    try:
        return venues[int(choice) - 1]
    except (ValueError, IndexError):
        console.print("[red]無効な入力です[/red]")
        return None


# ── Step 3: レース一覧取得・複数選択 ─────────────────────

def get_race_details(races: list[dict]) -> list[dict]:
    """各レースの名称・距離・発走情報を取得する"""
    console.print("\n[dim]レース情報を取得中...[/dim]")
    detailed = []
    for race in races:
        race_id = race["race_id"]
        url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            resp.encoding = resp.apparent_encoding
            soup = BeautifulSoup(resp.text, "lxml")

            # レース名
            name_tag = (soup.select_one("div.RaceName")
                        or soup.select_one("h1.RaceName")
                        or soup.select_one(".RaceMainColumn h1"))
            race_name = name_tag.get_text(strip=True) if name_tag else f"{race['race_no']}R"

            # 詳細（距離・発走時刻など）
            detail_tag = (soup.select_one("div.RaceData01")
                          or soup.select_one(".Race_Data"))
            detail = detail_tag.get_text(" ", strip=True) if detail_tag else ""
            # 余分な空白を整理
            detail = re.sub(r"\s+", " ", detail).strip()

        except Exception:
            race_name = f"{race['race_no']}R"
            detail = ""

        detailed.append({**race, "race_name": race_name, "detail": detail})
        time.sleep(DELAY)

    return detailed


def select_races(race_details: list[dict]) -> list[dict]:
    console.rule("[bold cyan]レース選択[/bold cyan]")

    t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 1))
    t.add_column("No.", width=4, justify="right")
    t.add_column("レース名", width=22)
    t.add_column("詳細", width=36)
    for r in race_details:
        t.add_row(str(r["race_no"]), r["race_name"], r["detail"])
    console.print(t)

    console.print("  [bold]カンマ区切り番号[/bold]  例: [cyan]1,3,11,12[/cyan]")
    console.print("  [bold]all[/bold]              全レース選択")
    console.print("  [bold]main[/bold]             特別競走・重賞のみ")

    choice = input("\n選択 [all]: ").strip().lower() or "all"

    if choice == "all":
        return race_details

    if choice == "main":
        keywords = ["GI", "GII", "GIII", "G1", "G2", "G3",
                    "特別", "ステークス", "カップ", "賞", "記念"]
        selected = [r for r in race_details
                    if any(kw in r["race_name"] for kw in keywords)]
        if not selected:
            console.print("[yellow]特別競走が見つかりません。全レースを選択します。[/yellow]")
            return race_details
        return selected

    try:
        nos = {int(n.strip()) for n in choice.split(",")}
        selected = [r for r in race_details if r["race_no"] in nos]
        if not selected:
            console.print("[yellow]該当レースなし。全レースを選択します。[/yellow]")
            return race_details
        return selected
    except ValueError:
        console.print("[red]無効な入力。全レースを選択します。[/red]")
        return race_details


# ── Step 4: オッズ取得・表示 ──────────────────────────────

def get_win_odds(race_id: str) -> list[dict]:
    """単勝オッズを取得する。各レコードに fetched_at_jst を付加する。
    JSON APIから取得し、馬名はshutuba.htmlから補完する。
    """
    fetched_at = now_jst()

    # ── 馬名を shutuba.html から取得 ──────────────────────────
    horse_names: dict[int, str] = {}
    try:
        shutuba_url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
        resp = requests.get(shutuba_url, headers=HEADERS, timeout=15)
        raw = resp.content.decode("euc-jp", errors="replace")
        soup = BeautifulSoup(raw, "lxml")
        for tr in soup.select("tr.HorseList"):
            umaban_td = tr.select_one("td[class*='Umaban']")
            name_tag = tr.select_one("span.HorseName a")
            if umaban_td and name_tag:
                try:
                    no = int(re.sub(r"\D", "", umaban_td.get_text(strip=True)))
                    horse_names[no] = name_tag.get_text(strip=True)
                except ValueError:
                    pass
    except Exception as e:
        console.print(f"[yellow]馬名取得失敗 ({race_id}): {e}[/yellow]")

    # ── 単勝オッズを JSON API から取得 ───────────────────────
    api_url = (f"https://race.netkeiba.com/api/api_get_jra_odds.html"
               f"?race_id={race_id}&type=1&action=update")
    try:
        resp = requests.get(api_url, headers=HEADERS, timeout=15)
        data = resp.json()
    except Exception as e:
        console.print(f"[red]オッズ取得失敗 ({race_id}): {e}[/red]")
        return []

    race_status = data.get("status", "unknown")  # "middle" / "result" / "before" etc.
    odds_dict = (data.get("data", {}).get("odds", {}).get("1", {}))
    results = []
    for horse_no_str, values in odds_dict.items():
        try:
            horse_no = int(horse_no_str)
            raw_odds = values[0] if values else ""
            if not raw_odds or re.fullmatch(r"[-]+\.?[-]*", raw_odds):
                odds = 0.0
            else:
                odds = float(raw_odds)
            results.append({
                "horse_no": horse_no,
                "horse_name": horse_names.get(horse_no, f"馬{horse_no}"),
                "win_odds": odds,
                "implied_prob": round(100.0 / odds, 1) if odds > 0 else 0.0,
                "fetched_at_jst": fetched_at,
                "race_status": race_status,
            })
        except (ValueError, IndexError):
            continue

    return sorted(results, key=lambda x: x["horse_no"])


def display_odds(race: dict, odds_list: list[dict], sort_by_odds: bool = False) -> None:
    if not odds_list:
        console.print(f"[dim]{race['race_name']}: オッズデータなし[/dim]")
        return

    data = (sorted(odds_list, key=lambda x: x["win_odds"] if x["win_odds"] > 0 else 999)
            if sort_by_odds else odds_list)

    fetched_at = data[0].get("fetched_at_jst", "") if data else ""
    t = Table(
        title=(f"[bold cyan]{race['race_name']}[/bold cyan]  "
               f"[dim]{race.get('detail', '')}[/dim]  "
               f"[dim]取得: {fetched_at}[/dim]"),
        box=box.SIMPLE_HEAVY, show_header=True, header_style="bold",
        padding=(0, 1),
    )
    t.add_column("馬番", width=5, justify="right")
    t.add_column("馬名", width=20)
    t.add_column("単勝", width=8, justify="right")
    t.add_column("確率", width=7, justify="right")
    t.add_column("評価", width=4, justify="center")

    for h in data:
        odds = h["win_odds"]
        prob = h["implied_prob"]
        if odds <= 0:
            style, mark = "dim", "  -"
        elif odds <= 3.0:
            style, mark = "bold green", " ◎"
        elif odds <= 6.0:
            style, mark = "green", " ○"
        elif odds <= 10.0:
            style, mark = "yellow", " ▲"
        elif odds <= 20.0:
            style, mark = "", " △"
        else:
            style, mark = "dim", "  ×"

        def markup(val: str) -> str:
            return f"[{style}]{val}[/{style}]" if style else val

        t.add_row(
            markup(f"{h['horse_no']:2d}"),
            markup(h["horse_name"]),
            markup(f"{odds:6.1f}") if odds > 0 else "    -",
            markup(f"{prob:5.1f}%") if prob > 0 else "     -",
            markup(mark),
        )
    console.print(t)


def _default_db_path() -> Path:
    """config.yaml があれば参照、なければ data/keiba.db を使う"""
    config_path = Path(__file__).parent.parent.parent / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            cfg = yaml.safe_load(config_path.read_text())
            rel = cfg["database"]["path"]
            return config_path.parent / rel
        except Exception:
            pass
    return Path(__file__).parent.parent.parent / "data" / "keiba.db"


def save_sqlite(all_data: list[dict], venue_name: str, db_path: Path | None = None) -> tuple[Path, int]:
    """
    win_odds_snapshot テーブルに単勝オッズを保存する。

    Returns:
        (db_path, 保存件数)
    複合ユニークキー (race_id, horse_no, fetched_at_jst) が衝突した場合は
    INSERT OR IGNORE でスキップする（同一取得結果の二重保存を防ぐ）。
    """
    if db_path is None:
        db_path = _default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS win_odds_snapshot (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id       TEXT    NOT NULL,
            horse_no      INTEGER NOT NULL,
            horse_name    TEXT,
            win_odds      REAL,
            implied_prob  REAL,
            venue         TEXT,
            race_name     TEXT,
            race_detail   TEXT,
            fetched_at_jst TEXT NOT NULL,
            UNIQUE(race_id, horse_no, fetched_at_jst)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_odds_snap_race "
        "ON win_odds_snapshot(race_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_odds_snap_fetched "
        "ON win_odds_snapshot(fetched_at_jst)"
    )

    saved = 0
    for item in all_data:
        race = item["race"]
        for h in item["odds"]:
            if not h.get("win_odds") or h["win_odds"] <= 0:
                continue  # 未確定オッズは保存しない
            cursor = conn.execute(
                """INSERT OR IGNORE INTO win_odds_snapshot
                   (race_id, horse_no, horse_name, win_odds, implied_prob,
                    venue, race_name, race_detail, fetched_at_jst)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    race["race_id"],
                    h["horse_no"],
                    h.get("horse_name"),
                    h.get("win_odds"),
                    h.get("implied_prob"),
                    venue_name,
                    race.get("race_name"),
                    race.get("detail"),
                    h.get("fetched_at_jst"),
                ),
            )
            saved += cursor.rowcount
    conn.commit()
    conn.close()
    return db_path, saved


def save_csv(all_data: list[dict], date_str: str, venue_name: str) -> Path:
    out_dir = Path("data/raw")
    out_dir.mkdir(parents=True, exist_ok=True)
    display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    out_path = out_dir / f"odds_{date_str}_{venue_name}.csv"

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "date", "venue", "race_no", "race_name", "detail",
            "horse_no", "horse_name", "win_odds", "implied_prob",
        ])
        writer.writeheader()
        for item in all_data:
            for h in item["odds"]:
                if not h.get("win_odds") or h["win_odds"] <= 0:
                    continue  # 未確定オッズは保存しない
                writer.writerow({
                    "date": display_date,
                    "venue": venue_name,
                    "race_no": item["race"]["race_no"],
                    "race_name": item["race"]["race_name"],
                    "detail": item["race"].get("detail", ""),
                    **h,
                })
    return out_path


def save_csv_auto(venues_data: list[dict], date_str: str, timestamp: str) -> Path:
    """
    全場所分のオッズを1つのCSVに保存する（自動モード用）。
    ファイル名: data/raw/odds_YYYYMMDD_HHMMSS.csv
    venues_data: [{"venue": venue_dict, "all_data": [...]}]
    """
    out_dir = Path("data/raw")
    out_dir.mkdir(parents=True, exist_ok=True)
    display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    out_path = out_dir / f"odds_{date_str}_{timestamp}.csv"

    fieldnames = [
        "date", "venue", "race_no", "race_name", "detail",
        "horse_no", "horse_name", "win_odds", "implied_prob",
        "fetched_at_jst", "race_status",
    ]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for vd in venues_data:
            venue_name = vd["venue"]["name"]
            for item in vd["all_data"]:
                for h in item["odds"]:
                    if not h.get("win_odds") or h["win_odds"] <= 0:
                        continue
                    writer.writerow({
                        "date": display_date,
                        "venue": venue_name,
                        "race_no": item["race"]["race_no"],
                        "race_name": item["race"]["race_name"],
                        "detail": item["race"].get("detail", ""),
                        "horse_no": h["horse_no"],
                        "horse_name": h["horse_name"],
                        "win_odds": h["win_odds"],
                        "implied_prob": h["implied_prob"],
                        "fetched_at_jst": h.get("fetched_at_jst", ""),
                        "race_status": h.get("race_status", ""),
                    })
    return out_path


# ── 取得ロジック（手動・自動共通） ───────────────────────

def fetch_all_races(
    selected: list[dict],
    venue_name: str,
    sort_by_odds: bool = False,
    silent: bool = False,
) -> list[dict]:
    """
    選択済みレース全件のオッズを取得して返す。
    silent=True のときは display_odds を呼ばない（自動ループ用）。
    """
    all_data = []
    for race in selected:
        race_id = race.get("race_id", "")
        if not race_id:
            continue
        if not silent:
            console.print(f"[dim]  {race['race_name']} を取得中...[/dim]")
        odds_list = get_win_odds(race_id)
        if not silent:
            display_odds(race, odds_list, sort_by_odds=sort_by_odds)
        # オッズが1件以上確定している場合のみ保存対象に含める
        valid_odds = [h for h in odds_list if h.get("win_odds", 0) > 0]
        if valid_odds:
            all_data.append({"race": race, "odds": valid_odds})
        elif odds_list and not silent:
            console.print(f"  [dim yellow]{race['race_name']}: オッズ未確定のためスキップ[/dim yellow]")
        time.sleep(DELAY)
    return all_data


# ── 自動ループ ────────────────────────────────────────────

def auto_loop(
    selected: list[dict],
    venue: dict,
    date_str: str,
    interval_min: int,
    sort_by_odds: bool = False,
) -> None:
    """
    interval_min 分ごとにオッズを取得して SQLite に自動保存する。
    Ctrl+C で停止。
    """
    from rich.live import Live
    from rich.text import Text

    interval_sec = interval_min * 60
    venue_name = venue["name"]
    display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    race_labels = ", ".join(r["race_name"] for r in selected)

    total_saved = 0
    loop_count = 0
    db_path = _default_db_path()

    console.rule(
        f"[bold cyan]自動取得モード  {venue_name}  {display_date}  "
        f"間隔: {interval_min}分[/bold cyan]"
    )
    console.print(f"対象: [cyan]{race_labels}[/cyan]")
    console.print("[dim]停止するには Ctrl+C を押してください[/dim]\n")

    def _run_once() -> int:
        nonlocal loop_count, total_saved
        loop_count += 1
        ts = now_jst()
        console.print(
            f"[bold]#{loop_count}[/bold]  取得開始  [dim]{ts}[/dim]"
        )
        all_data = fetch_all_races(selected, venue_name, sort_by_odds=sort_by_odds, silent=False)
        if all_data:
            _, saved = save_sqlite(all_data, venue_name, db_path=db_path)
            total_saved += saved
            console.print(
                f"  → SQLite保存: [green]{saved}件[/green]  "
                f"累計: [green]{total_saved}件[/green]  "
                f"DB: [dim]{db_path}[/dim]"
            )
        return interval_sec

    try:
        while True:
            wait_sec = _run_once()

            # カウントダウン表示
            with Live(console=console, refresh_per_second=2) as live:
                for remaining in range(wait_sec, 0, -1):
                    next_time = datetime.now(JST) + timedelta(seconds=remaining)
                    live.update(
                        Text(
                            f"  次の取得まで  {remaining // 60:02d}:{remaining % 60:02d}  "
                            f"(次回: {next_time.strftime('%H:%M:%S')})",
                            style="dim",
                        )
                    )
                    time.sleep(1)

    except KeyboardInterrupt:
        console.print(
            f"\n[bold yellow]自動取得を停止しました。"
            f"  取得回数: {loop_count}  累計保存: {total_saved}件[/bold yellow]"
        )


# ── 自動モード（GitHub Actions 用） ──────────────────────

def auto_main(preset_date: str | None = None) -> None:
    """
    非対話モード: 全場所・全レースのオッズを取得してCSVに保存する。
    GitHub Actions から --auto で呼び出す。
    """
    date_str = (preset_date or "").replace("-", "") or date.today().strftime("%Y%m%d")
    display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    timestamp = datetime.now(JST).strftime("%H%M%S")

    console.print(f"[bold cyan]自動モード開始  {display_date}[/bold cyan]")

    venues = get_venues(date_str)
    if not venues:
        console.print("[yellow]開催情報なし。終了します。[/yellow]")
        return

    console.print(f"開催場所: {', '.join(v['name'] for v in venues)}")

    venues_data = []
    for venue in venues:
        console.print(f"\n[bold]{venue['name']}[/bold] のレース情報を取得中...")
        race_details = get_race_details(venue["races"])
        all_data = fetch_all_races(race_details, venue["name"], silent=False)
        if all_data:
            venues_data.append({"venue": venue, "all_data": all_data})

    if not venues_data:
        console.print("[yellow]オッズデータなし（開催前 or 全レース終了）。[/yellow]")
        return

    out_path = save_csv_auto(venues_data, date_str, timestamp)
    total = sum(
        len(item["odds"])
        for vd in venues_data
        for item in vd["all_data"]
    )
    console.print(f"\n[green]CSV保存完了: {out_path}  ({total}件)[/green]")


# ── メインフロー ──────────────────────────────────────────

def main(preset_date: str | None = None, preset_interval: int = 0) -> None:
    # Step 1: 日付
    date_str = select_date(preset_date)
    display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    # Step 2: 競馬場
    console.print(f"\n[dim]{display_date} の開催情報を取得中...[/dim]")
    venues = get_venues(date_str)
    venue = select_venue(venues)
    if not venue:
        sys.exit(1)

    # Step 3: レース選択
    race_details = get_race_details(venue["races"])
    selected = select_races(race_details)
    if not selected:
        console.print("[yellow]レースが選択されていません[/yellow]")
        sys.exit(1)

    # ソート設定
    sort_input = input("\nオッズを低い順に並べ替えますか? (y/N): ").strip().lower()
    sort_by_odds = sort_input == "y"

    # 自動取得モードの確認
    interval_min = preset_interval
    if interval_min == 0:
        interval_input = input(
            "\n自動取得の間隔を分単位で入力してください (0 = 手動で1回のみ): "
        ).strip()
        try:
            interval_min = int(interval_input)
        except ValueError:
            interval_min = 0

    # ── 自動ループ ──
    if interval_min > 0:
        auto_loop(selected, venue, date_str, interval_min, sort_by_odds=sort_by_odds)
        return

    # ── 手動（1回取得） ──
    console.rule(
        f"[bold cyan]{venue['name']}  {display_date}  "
        f"({len(selected)}レース) オッズ[/bold cyan]"
    )
    all_data = fetch_all_races(selected, venue["name"], sort_by_odds=sort_by_odds)

    if all_data:
        db_input = input("\nSQLiteに保存しますか? (Y/n): ").strip().lower()
        if db_input != "n":
            db_path, saved = save_sqlite(all_data, venue["name"])
            console.print(f"[green]SQLite保存完了: {db_path}  ({saved}件)[/green]")

        csv_input = input("CSVにも保存しますか? (y/N): ").strip().lower()
        if csv_input == "y":
            out = save_csv(all_data, date_str, venue["name"])
            console.print(f"[green]CSV保存完了: {out}[/green]")

    console.print("\n[bold green]完了[/bold green]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JRA オッズ取得")
    parser.add_argument("--date", default=None, help="日付 YYYYMMDD または YYYY-MM-DD")
    parser.add_argument(
        "--interval", type=int, default=0,
        help="自動取得の間隔（分）。0 = 手動で1回のみ (例: --interval 10)",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="非対話モード: 全場所・全レース取得してCSV保存 (GitHub Actions 用)",
    )
    args = parser.parse_args()
    if args.auto:
        auto_main(preset_date=args.date)
    else:
        main(preset_date=args.date, preset_interval=args.interval)
