"""
🏆 DMM同人ランキング → X投稿下書き生成ツール
================================================================
DMM Webサービス（アフィリエイト）APIの ItemList エンドポイントから
「同人」カテゴリーの人気ランキングを取得し、上位5件（既出作品は除外して繰り下げ）分の
X投稿用スレッド下書き（テキスト）をコンソールに出力します。

⚠️ 重要：このスクリプトは自動投稿を一切行いません（AUTO_POST機能なし）。
   出力された下書きの内容を必ず人間が確認し、内容が適切であることを確かめたうえで
   手動でXに投稿してください。

■ 投稿スレッド構成（1作品あたり5投稿）
  投稿1（メイン）   : サンプル画像1枚目 + 作品タイトル等
  リプライ2         : サンプル画像2枚目
  リプライ3         : サンプル画像3枚目
  リプライ4         : サンプル画像4枚目
  リプライ5         : サンプル画像5枚目 + アフィリエイトURL

■ 必須環境変数
  DMM_API_ID        : DMM Webサービスの API ID
  DMM_AFFILIATE_ID  : DMMアフィリエイトID（例: xxxxx-990）

■ 任意環境変数
  DMM_FLOOR         : 検索対象フロア（デフォルト 'doujin'）
  RANK_COUNT        : 何位まで下書きを作るか（デフォルト 5）
  FETCH_HITS        : APIから一度に取得する件数（デフォルト 30。重複除外後に5件残らない場合は増やす）
  HISTORY_KEEP_DAYS : 重複判定のため投稿済み履歴を保持する日数（デフォルト 30）
  OUTPUT_DIR        : 下書きファイルの出力先（デフォルト ./outputs）
"""

import os
import sys
import json
import datetime
import requests
from pathlib import Path

# ================================================================
# ⚙️ 設定
# ================================================================

DMM_API_ID = os.environ.get('DMM_API_ID', '').strip()
DMM_AFFILIATE_ID = os.environ.get('DMM_AFFILIATE_ID', '').strip()
DMM_FLOOR = os.environ.get('DMM_FLOOR', 'doujin').strip()
RANK_COUNT = int(os.environ.get('RANK_COUNT', '5'))
FETCH_HITS = int(os.environ.get('FETCH_HITS', '30'))
HISTORY_KEEP_DAYS = int(os.environ.get('HISTORY_KEEP_DAYS', '30'))

DMM_API_ENDPOINT = 'https://api.dmm.com/affiliate/v3/ItemList'
DMM_FLOORLIST_ENDPOINT = 'https://api.dmm.com/affiliate/v3/FloorList'
IMAGES_PER_ITEM = 5  # 投稿1本 + リプライ4本 = 5枚使用

if not DMM_API_ID or not DMM_AFFILIATE_ID:
    print('❌ DMM_API_ID / DMM_AFFILIATE_ID が未設定です。環境変数を設定してください。')
    sys.exit(1)


def get_output_dir():
    base = os.environ.get('OUTPUT_DIR', '').strip()
    p = Path(base) if base else Path(__file__).resolve().parent / 'outputs'
    p.mkdir(parents=True, exist_ok=True)
    return p


HISTORY_FILE = get_output_dir() / 'dmm_doujin_ranking_history.json'


# ================================================================
# 📁 履歴管理（重複除外用：content_id ベース）
# ================================================================

def load_history():
    if not HISTORY_FILE.exists():
        return []
    try:
        raw = json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []
    cutoff = (datetime.date.today() - datetime.timedelta(days=HISTORY_KEEP_DAYS)).isoformat()
    return [h for h in raw if h.get('date', '') >= cutoff]


def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')


# ================================================================
# 🌐 DMM API 呼び出し
# ================================================================

def resolve_floor_code():
    """FloorList APIから site=FANZA / service(またはfloor)=DMM_FLOOR に対応する
    正式な service名 と floorコードを取得する。見つからない場合は候補一覧を表示して終了する。"""
    params = {
        'api_id': DMM_API_ID,
        'affiliate_id': DMM_AFFILIATE_ID,
        'output': 'json',
    }
    try:
        resp = requests.get(DMM_FLOORLIST_ENDPOINT, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f'❌ FloorList APIへのリクエストに失敗しました: {e}')
        sys.exit(1)

    sites = data.get('result', {}).get('site', [])
    candidates = []  # (site_name, service_name, floor_id, floor_code, floor_name)

    for site in sites:
        site_name = site.get('name', '')
        for service in site.get('service', []):
            service_name = service.get('name', '')
            for floor in service.get('floor', []):
                candidates.append((
                    site_name,
                    service_name,
                    floor.get('id'),
                    floor.get('code'),
                    floor.get('name'),
                ))

    # 1) site=FANZA かつ service名がDMM_FLOORと完全一致するものを優先
    for site_name, service_name, floor_id, floor_code, floor_name in candidates:
        if site_name == 'FANZA' and service_name == DMM_FLOOR:
            return service_name, floor_code

    # 2) floorコード自体がDMM_FLOORと一致するもの
    for site_name, service_name, floor_id, floor_code, floor_name in candidates:
        if site_name == 'FANZA' and floor_code == DMM_FLOOR:
            return service_name, floor_code

    print(f'❌ site=FANZA 内に service/floor = "{DMM_FLOOR}" が見つかりませんでした。')
    print('   利用可能な FANZA の service / floor 一覧:')
    for site_name, service_name, floor_id, floor_code, floor_name in candidates:
        if site_name == 'FANZA':
            print(f'   - service={service_name:<15} floor={floor_code:<15} ({floor_name})')
    sys.exit(1)


def fetch_ranking(hits=FETCH_HITS):
    """DMM ItemList APIから人気順（rank）の同人ランキングを取得する。"""
    service_name, floor_code = resolve_floor_code()
    print(f'ℹ️ 使用する service={service_name} / floor={floor_code}')

    params = {
        'api_id': DMM_API_ID,
        'affiliate_id': DMM_AFFILIATE_ID,
        'site': 'FANZA',
        'service': service_name,
        'floor': floor_code,
        'hits': hits,
        'sort': 'rank',
        'output': 'json',
    }
    resp = requests.get(DMM_API_ENDPOINT, params=params, timeout=30)
    if resp.status_code != 200:
        print(f'❌ DMM APIへのリクエストに失敗しました: {resp.status_code} {resp.reason}')
        print(f'   リクエストURL: {resp.url}')
        print(f'   レスポンス本文: {resp.text[:1000]}')
        sys.exit(1)
    try:
        data = resp.json()
    except Exception as e:
        print(f'❌ レスポンスのJSON解析に失敗しました: {e}')
        print(f'   レスポンス本文: {resp.text[:1000]}')
        sys.exit(1)

    result = data.get('result', {})
    if result.get('status') and str(result.get('status')) != '200':
        print(f'❌ DMM APIエラー: {result}')
        sys.exit(1)

    return result.get('items', [])


def extract_sample_images(item, max_images=IMAGES_PER_ITEM):
    """商品データからサンプル画像URLを最大 max_images 枚取り出す。"""
    images = []
    sample = (item.get('sampleImageURL') or {})
    # DMM APIは 'sample_s'（小）や 'sample_l'（大）にimage配列を持つ
    for key in ('sample_l', 'sample_s'):
        block = sample.get(key)
        if block and block.get('image'):
            images = block['image']
            break
    if not images:
        # フォールバック：パッケージ画像だけでも入れておく
        pkg = (item.get('imageURL') or {}).get('large') or (item.get('imageURL') or {}).get('list')
        if pkg:
            images = [pkg]
    return images[:max_images]


def build_ranked_list_excluding_duplicates(items, used_ids, rank_count):
    """既出（used_idsに含まれる）content_idをスキップしながら、
    ランキング順に rank_count 件選び出す（自動で順位が繰り下がる）。"""
    picked = []
    display_rank = 0
    for item in items:
        display_rank += 1
        content_id = item.get('content_id') or item.get('product_id')
        if not content_id:
            continue
        if content_id in used_ids:
            continue
        picked.append((display_rank, item))
        if len(picked) >= rank_count:
            break
    return picked


# ================================================================
# 📝 投稿スレッド下書きの組み立て
# ================================================================

def build_thread_draft(rank, item):
    title = item.get('title', '(タイトル不明)')
    affiliate_url = item.get('affiliateURL') or item.get('URL', '')
    images = extract_sample_images(item)

    while len(images) < IMAGES_PER_ITEM:
        images.append('(サンプル画像なし)')

    posts = []
    # 投稿1（メイン）
    posts.append({
        'label': f'投稿1（メイン）',
        'text': f'【本日の同人ランキング {rank}位】\n{title}',
        'image': images[0],
    })
    # リプライ2〜4
    for i in range(1, 4):
        posts.append({
            'label': f'リプライ{i + 1}',
            'text': '',
            'image': images[i],
        })
    # リプライ5（アフィリエイトURL付き）
    posts.append({
        'label': 'リプライ5',
        'text': f'▼詳細・購入はこちら\n{affiliate_url}',
        'image': images[4],
    })

    return posts


def print_thread_draft(rank, item, posts):
    content_id = item.get('content_id') or item.get('product_id') or '(ID不明)'
    print('\n' + '-' * 60)
    print(f'🏅 {rank}位: {item.get("title", "(タイトル不明)")}  [content_id: {content_id}]')
    print('-' * 60)
    for p in posts:
        print(f'\n  ▶ {p["label"]}')
        if p['text']:
            print(f'    本文: {p["text"]}')
        print(f'    画像: {p["image"]}')


# ================================================================
# 🚀 メイン処理
# ================================================================

def main():
    history = load_history()
    used_ids = {h['content_id'] for h in history}

    print('=' * 60)
    print(f'📋 DMM同人ランキング下書き生成（対象: 上位{RANK_COUNT}件・重複除外）')
    print('=' * 60)

    items = fetch_ranking(hits=FETCH_HITS)
    if not items:
        print('❌ ランキングデータを取得できませんでした。')
        sys.exit(1)

    picked = build_ranked_list_excluding_duplicates(items, used_ids, RANK_COUNT)

    if len(picked) < RANK_COUNT:
        print(
            f'⚠️ 重複除外後、{len(picked)}件しか選べませんでした（取得件数 FETCH_HITS={FETCH_HITS} を'
            f'増やすと改善する場合があります）。'
        )

    new_history_entries = []

    for display_rank, item in picked:
        content_id = item.get('content_id') or item.get('product_id')
        posts = build_thread_draft(display_rank, item)
        print_thread_draft(display_rank, item, posts)

        new_history_entries.append({
            'content_id': content_id,
            'title': item.get('title', ''),
            'date': datetime.date.today().isoformat(),
        })

    history.extend(new_history_entries)
    save_history(history)

    print('\n' + '=' * 60)
    print(f'✅ 完了: {len(picked)}件の下書きをコンソールに出力しました（自動投稿はしていません）。')
    print('   内容を確認のうえ、手動でXに投稿してください。')
    print(f'📚 履歴ファイル（重複判定用）: {HISTORY_FILE}')
    print('=' * 60)


if __name__ == '__main__':
    main()
