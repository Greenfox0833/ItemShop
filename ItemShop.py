import json
import sys
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

URL = "https://fortnitecontent-website-prod07.ol.epicgames.com/content/api/pages/fortnite-game/mp-item-shop"
PARAMS = {"lang": "ja"}
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json"
}

def dig_for_sections(obj):
    if isinstance(obj, dict):
        for k in ["shopSections", "sections", "sectionList", "ShopSections", "shop_sections"]:
            if k in obj and isinstance(obj[k], list):
                return obj[k]
        for v in obj.values():
            res = dig_for_sections(v)
            if res:
                return res
    elif isinstance(obj, list):
        for v in obj:
            res = dig_for_sections(v)
            if res:
                return res
    return None

def normalize_section(sec):
    # ネスト（section / content）を吸収
    base = sec.get("section") or sec.get("content") or sec
    meta = base.get("metadata") or sec.get("metadata") or {}

    get = lambda *keys: next((base[k] for k in keys if isinstance(base, dict) and k in base), None)

    # --- stackRanks を集約：直下 / base / metadata / offerGroups[].stackRanks ---
    stackRanks_all = []
    for src in (sec.get("stackRanks"), base.get("stackRanks"), meta.get("stackRanks")):
        if isinstance(src, list):
            stackRanks_all.extend(src)

    # offerGroups 側の stackRanks も追加
    og_list = meta.get("offerGroups") or []
    for og in og_list:
        if isinstance(og, dict):
            ranks = og.get("stackRanks")
            if not ranks:
                ranks = (og.get("metadata") or {}).get("stackRanks")
            if isinstance(ranks, list):
                stackRanks_all.extend(ranks)

    # ① デフォルトの 2023-01-01 は除外
    # ① デフォルトの 2023-01-01 は除外しつつ、グループごとに開始/終了を決定
    #    グループキー: (context, productTag)
    groups = {}
    for s in stackRanks_all:
        sd = s.get("startDate")
        if not sd or sd == "2023-01-01T00:00:00.000Z":
            continue
        key = (s.get("context"), s.get("productTag"))
        groups.setdefault(key, []).append(s)

    parsed_ranks = []
    for key, items in groups.items():
        # ISO8601文字列なので文字列ソートでOK（厳密にするなら datetime にしても良い）
        items.sort(key=lambda x: x.get("startDate"))
        for i, cur in enumerate(items):
            nxt = items[i + 1] if i + 1 < len(items) else None
            parsed_ranks.append({
                # 出力は開始日と終了日のみ（終了日は次の開始日、最後は None）
                "startDate": cur.get("startDate"),
                "endDate":   nxt.get("startDate") if nxt else None,
            })


    # ② 背景URL（customTexture）
    bg = (meta.get("background") or {})
    custom_tex = bg.get("customTexture")

    # ③ offerGroups 件数（同一 sectionId 内で offerGroupId 重複は 1 カウントに集約）
    og_list = meta.get("offerGroups") or []
    unique_ids = set()
    no_id_count = 0
    for og in og_list:
        if not isinstance(og, dict):
            no_id_count += 1
            continue
        # 通常は og["offerGroupId"] を参照。念のため metadata 側も見る。
        oid = og.get("offerGroupId")
        if not oid and isinstance(og.get("metadata"), dict):
            oid = og["metadata"].get("offerGroupId")
        if oid:
            unique_ids.add(str(oid))
        else:
            # ID が無いものは個別カウント
            no_id_count += 1
    offer_groups_count = len(unique_ids) + no_id_count

    # ④ textureMetadata の有無とURLリスト（保存用）
    texture_urls = []
    for og in og_list:
        md = (og.get("metadata") or {})
        tmetas = md.get("textureMetadata") or []
        for t in tmetas:
            val = t.get("value")
            if isinstance(val, str):
                texture_urls.append(val)
    has_texture_metadata = len(texture_urls) > 0
    # 重複除去
    texture_urls = list(dict.fromkeys(texture_urls))

    # stackRanks の開始日だけを1つ取る
    start_date = None
    if parsed_ranks:
        try:
            start_date = min(r["startDate"] for r in parsed_ranks if r.get("startDate"))
        except ValueError:
            start_date = None

    return {
        "sectionId":          get("sectionId", "sectionID", "id", "section_id", "name"),
        "displayName":        get("displayName", "title", "sectionDisplayName"),
        "customTexture":      custom_tex,
        "offerGroupsCount":   offer_groups_count,      # ① 追加
        "hasTextureMetadata": has_texture_metadata,    # ② 追加（True/False）
        "textureUrls":        texture_urls,            # ② 保存用URLリスト
        "stackRankStart":     start_date,
    }

def main():
    r = requests.get(URL, params=PARAMS, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()

    sections = dig_for_sections(data)
    if not sections:
        print("ショップセクションが見つかりません。response.jsonを確認してください。")
        with open("response.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        sys.exit(1)

    normalized = [normalize_section(s) for s in sections]

    # 出力
    # === 日付ごと(JSON)の分割出力 ===
    out_dir = Path("itemshop_by_date")
    out_dir.mkdir(parents=True, exist_ok=True)

    JST = timezone(timedelta(hours=9))  # Asia/Tokyo

    def to_jst_date_str(iso_str: str) -> str | None:
        if not iso_str:
            return None
        try:
            # 例: "2025-09-19T00:00:00.000Z"
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            dt_jst = dt.astimezone(JST)
            return dt_jst.strftime("%Y-%m-%d")
        except Exception:
            return None

    # 日付ごとにまとめる（stackRankStartが無いものはスキップ）
    bucket: dict[str, list] = {}
    for row in normalized:
        d = to_jst_date_str(row.get("stackRankStart"))
        if not d:
            continue
        bucket.setdefault(d, []).append(row)

    # ファイル出力: mp_item_shop_YYYY-MM-DD.json
    generated_at = datetime.now(JST).isoformat()
    for d, rows in bucket.items():
        payload = {
            "date": d,                 # JSTの日付
            "generatedAt": generated_at,
            "count": len(rows),
            "sections": rows,
        }
        with open(out_dir / f"mp_item_shop_{d}.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"🗂️ 分割JSONを書き出しました: {out_dir} （{len(bucket)}ファイル）")

    import os
    from urllib.parse import urlparse

    # 画像保存（URL名で保存）
    base_dir = Path("itemshop_backgrounds")
    base_dir.mkdir(parents=True, exist_ok=True)

    def url_basename(url: str) -> str:
        path = urlparse(url).path
        fname = os.path.basename(path)
        return fname or "unknown.jpg"

    def section_subdir(row):
        # textureMetadata がある場合は sectionID（または displayName）でサブフォルダを作成
        if row.get("hasTextureMetadata"):
            folder = row.get("sectionId") or row.get("displayName") or "unknown_section"
            folder = "".join(c for c in str(folder) if c not in r'\/:*?"<>|').strip()
            return base_dir / folder
        return base_dir

    def save_url_to(url: str, dest_dir: Path):
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            out_path = dest_dir / url_basename(url)
            if out_path.exists():
                return
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            with open(out_path, "wb") as f:
                f.write(resp.content)
            print(f"🖼️ Saved: {out_path}")
        except Exception as e:
            print(f"[WARN] 画像保存失敗: {url} -> {e}")

    for row in normalized:
        dest = section_subdir(row)
        # customTexture を保存
        if row.get("customTexture"):
            save_url_to(row["customTexture"], dest)
        # textureMetadata のURL群も保存
        for tu in row.get("textureUrls", []):
            save_url_to(tu, dest)

    # TSV出力（stackRanks は日付と値をまとめて1セルに）
    with open("shop_sections_with_dates.tsv", "w", encoding="utf-8") as f:
        f.write("sectionId\tdisplayName\tlandingPriority\tsortPriority\tdevName\tcustomTexture\tofferGroupsCount\ttextureMetadata\tstackRankStart\n")
        for row in normalized:
            f.write("\t".join([
                str(row.get("sectionId", "")),
                str(row.get("displayName", "")),
                str(row.get("customTexture", "")),
                str(row.get("offerGroupsCount", "")),         # ① 追加
                str(row.get("hasTextureMetadata", "")),       # ② 追加
                str(row.get("stackRankStart", "")),
            ]) + "\n")

    print("✅ shop_sections_with_dates.json / shop_sections_with_dates.tsv を出力しました。")

if __name__ == "__main__":
    main()
