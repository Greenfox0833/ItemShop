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

    # ③ offerGroups 件数（= セクション内サブセクション数）
    og_list = meta.get("offerGroups") or []
    offer_groups_count = len(og_list)

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

    for row in normalized:
        create_section_image(row, Path("itemshop_section_images"))

from PIL import Image, ImageDraw, ImageFont
import io

FONT_PATH = "c:/USERS/FN_GREENFOX/APPDATA/LOCAL/MICROSOFT/WINDOWS/FONTS/NOTOSANSJP-BOLD.OTF"  # 日本語表示用

from PIL import Image, ImageDraw, ImageFont, ImageFilter
import io, math, textwrap

FONT_PATH_TITLE = FONT_PATH  # タイトル用（必要なら太字フォントに変更）
FONT_PATH_INFO  = FONT_PATH  # 情報用

def _wrap_text(draw, text, font, max_width):
    """max_width を超えないように日本語もざっくり折り返し"""
    # textwrap は英語向けだが、幅で落ちやすいよう短めで折る
    lines = []
    if not text:
        return [""]
    # まず適当な目安で幅推定 → 少しずつ詰める
    est = max(8, min(len(text), 28))
    for trial in range(est, 4, -1):
        test = textwrap.wrap(text, width=trial, break_long_words=True, drop_whitespace=False)
        if all(draw.textlength(t, font=font) <= max_width for t in test):
            lines = test
            break
    if not lines:
        # 最後の砦：1文字ずつ積んで折り返し
        cur = ""
        for ch in text:
            if draw.textlength(cur + ch, font=font) <= max_width:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
    return lines

def create_section_image(row, out_dir: Path):
    bg_url = row.get("customTexture")
    if not bg_url:
        return

    # ==== 背景取得 & リサイズ ====
    try:
        resp = requests.get(bg_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        bg_img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
        bg_img = bg_img.resize((750, 422), Image.LANCZOS)
    except Exception as e:
        print(f"[WARN] 背景取得失敗: {bg_url} -> {e}")
        return

    W, H = 750, 422

    # ==== 上から下に黒フェード ====
    grad = Image.new("L", (1, H))
    for y in range(H):
        alpha = int((y / H) * 200)  # 下に行くほど濃く
        grad.putpixel((0, y), alpha)
    grad = grad.resize((W, H))
    fade = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    bg_img = Image.alpha_composite(bg_img, Image.merge("RGBA", (*fade.split()[:3], grad)))

    draw = ImageDraw.Draw(bg_img)

    # ==== フォント ====
    try:
        font_title = ImageFont.truetype(FONT_PATH_TITLE, 56)
        font_info  = ImageFont.truetype(FONT_PATH_INFO, 32)
    except Exception:
        font_title = font_info = ImageFont.load_default()

    # ==== 日付変換 ====
    release_fmt = "-"
    raw = row.get("stackRankStart", "")
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            dt_jst = dt.astimezone(timezone(timedelta(hours=9)))
            release_fmt = f"{dt_jst.month}月{dt_jst.day}日"
        except Exception:
            release_fmt = raw

    # ==== テキスト準備 ====
    display_name = row.get("displayName") or "無題セクション"
    section_id   = row.get("sectionId") or "unknown_id"
    groups_cnt   = int(row.get("offerGroupsCount") or 0)

    # ==== タイトル（中央上部、大きく）====
    tw = draw.textlength(display_name, font=font_title)
    draw.text(((W - tw) // 2, 28), display_name, font=font_title,
              fill=(255, 255, 255, 240), stroke_width=3, stroke_fill=(0, 0, 0, 200))

    # ==== 中央に半透明パネル ====
    panel_w, panel_h = W - 80, 120
    panel_x, panel_y = (W - panel_w)//2, (H - panel_h)//2
    panel = Image.new("RGBA", (W, H), (0,0,0,0))
    pdraw = ImageDraw.Draw(panel)
    pdraw.rounded_rectangle((panel_x, panel_y, panel_x+panel_w, panel_y+panel_h),
                            radius=20, fill=(0,0,0,160))
    bg_img = Image.alpha_composite(bg_img, panel)
    draw = ImageDraw.Draw(bg_img)

    # ==== 情報3行 ====
    info_lines = [
        f"🆔 {section_id}",
        f"#️⃣ {groups_cnt} セクション",
        f"📅 {release_fmt}",
    ]
    iy = panel_y + 20
    for line in info_lines:
        draw.text((panel_x+30, iy), line, font=font_info,
                  fill=(255,255,255,240), stroke_width=2, stroke_fill=(0,0,0,180))
        iy += font_info.size + 10

    # ==== 外枠 ====
    border = ImageDraw.Draw(bg_img)
    border.rounded_rectangle((4,4,W-4,H-4), radius=24,
                             outline=(255,255,255,60), width=3)

    # ==== 保存 ====
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = "".join(c for c in f"{section_id or display_name}.png" if c not in r'\/:*?\"<>|')
    final_path = out_dir / fname
    tmp_path = final_path.with_name(final_path.name + ".__tmp")
    try:
        bg_img.save(tmp_path, format="PNG")
        tmp_path.replace(final_path)
    finally:
        if tmp_path.exists():
            try: tmp_path.unlink()
            except: pass
    print(f"🖼️ セクション画像を保存しました: {final_path}")

if __name__ == "__main__":
    main()
