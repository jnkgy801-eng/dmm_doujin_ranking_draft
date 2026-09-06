"""
🏆 DMM同人ランキング → X本投稿ツール（Buffer 新API・自動公開版）
================================================================
DMM Webサービス（アフィリエイト）APIの ItemList エンドポイントから
「同人」カテゴリーの人気ランキングを取得し、上位3件（既出作品は除外して繰り下げ）分の
X投稿用スレッドをコンソールに出力し、Buffer連携が有効な場合はBufferのキューに
登録して自動公開します。

⚠️ 重要：BUFFER_API_KEY / BUFFER_CHANNEL_IDS を設定すると、生成したスレッドは
   人間の承認を挟まずBufferのキューに登録され、Buffer側の投稿スケジュールに従って
   自動的にXへ公開されます（saveToDraft=false）。公開前に人間が内容を確認する
   ステップは存在しないため、実行前に投稿内容・アカウント設定を十分確認してください。

   ※ Xの自動化ポリシー・成人向けコンテンツポリシーに抵触するリスクがあるため、
      本番運用する場合はアカウント側の設定（センシティブコンテンツ表示設定等）や
      Xの利用規約を事前にご確認ください。

   ※ 旧Buffer Publish API（api.bufferapp.com/1/...）は2025年1月3日をもって
      廃止されており、現在は新しいBuffer API（GraphQL, https://api.buffer.com,
      Bearer認証, 個人APIキーは https://publish.buffer.com/settings/api で発行）
      に統一されています。本スクリプトはこの新API向けに実装されています。

   ※ 新Buffer APIはXの「返信スレッド」をネイティブにサポートしています
      （createPost の metadata.twitter.thread フィールド）。そのため本スクリプトは
      1作品＝1回の createPost 呼び出しで、5投稿を正式なスレッドとして登録します。
      ※ Buffer APIは現状ベータ版のため、仕様が変更される可能性があります。
        実行前に https://developers.buffer.com/ で最新仕様をご確認ください。

■ 投稿スレッド構成（1作品あたり5投稿＝1スレッド）
  投稿1（メイン）   : サンプル画像1枚目 + 作品タイトル + (1/5)
  リプライ2         : サンプル画像2枚目 + (2/5)
  リプライ3         : サンプル画像3枚目 + (3/5)
  リプライ4         : サンプル画像4枚目 + (4/5)
  リプライ5         : サンプル画像5枚目 + アフィリエイトURL + (5/5)

■ 必須環境変数
  DMM_API_ID        : DMM Webサービスの API ID
  DMM_AFFILIATE_ID  : DMMアフィリエイトID（例: xxxxx-990）

■ 任意環境変数
  DMM_FLOOR         : 検索対象フロア（デフォルト 'doujin'）
  RANK_COUNT        : 1日に何件（何位まで）投稿するか（デフォルト 3）
  FETCH_HITS        : APIから一度に取得する件数（デフォルト 30。重複除外後に5件残らない場合は増やす）
  HISTORY_KEEP_DAYS : 重複判定のため投稿済み履歴を保持する日数（デフォルト 30）
  OUTPUT_DIR        : 下書きファイルの出力先（デフォルト ./outputs）

■ 任意環境変数（Buffer連携。両方設定した場合のみ有効）
  BUFFER_API_KEY      : Buffer個人APIキー（https://publish.buffer.com/settings/api で発行）
  BUFFER_CHANNEL_IDS  : 送信先BufferチャンネルID（Xチャンネル）をカンマ区切りで指定
                        （チャンネルIDは GraphQL の `channels` クエリで取得可能。
                          未設定の場合はBuffer連携をスキップし、従来どおりコンソール出力のみ行う）
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
RANK_COUNT = int(os.environ.get('RANK_COUNT', '3'))
FETCH_HITS = int(os.environ.get('FETCH_HITS', '30'))
HISTORY_KEEP_DAYS = int(os.environ.get('HISTORY_KEEP_DAYS', '30'))

# --- Buffer連携（任意・新API/GraphQL版）-----------------------------------
BUFFER_API_KEY = os.environ.get('BUFFER_API_KEY', '').strip()
BUFFER_CHANNEL_IDS = [
    cid.strip() for cid in os.environ.get('BUFFER_CHANNEL_IDS', '').split(',') if cid.strip()
]
BUFFER_ENABLED = bool(BUFFER_API_KEY and BUFFER_CHANNEL_IDS)
BUFFER_GRAPHQL_ENDPOINT = 'https://api.buffer.com'
# False: 下書き保存ではなく、Bufferのキューに正式なポストとして追加する。
# schedulingType='automatic' のため、Buffer側の投稿スケジュール（プランごとの
# 投稿時間枠）に従って自動的に公開される。人間の承認ステップは挟まらない。
BUFFER_SAVE_AS_DRAFT = False

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
    candidates = []  # (site_code, service_code, floor_id, floor_code, floor_name, site_name, service_name)

    for site in sites:
        site_code = site.get('code', site.get('name', ''))
        site_name = site.get('name', '')
        for service in site.get('service', []):
            service_code = service.get('code', service.get('name', ''))
            service_name = service.get('name', '')
            for floor in service.get('floor', []):
                candidates.append((
                    site_code,
                    service_code,
                    floor.get('id'),
                    floor.get('code'),
                    floor.get('name'),
                    site_name,
                    service_name,
                ))

    # 1) service名(表示名)またはservice_code が DMM_FLOOR と完全一致するもの
    for site_code, service_code, floor_id, floor_code, floor_name, site_name, service_name in candidates:
        if service_code == DMM_FLOOR or service_name == DMM_FLOOR:
            return site_code, service_code, floor_code

    # 2) floorコード自体がDMM_FLOORと一致するもの
    for site_code, service_code, floor_id, floor_code, floor_name, site_name, service_name in candidates:
        if floor_code == DMM_FLOOR:
            return site_code, service_code, floor_code

    # 3) service名/floor名にDMM_FLOORが部分一致するもの
    for site_code, service_code, floor_id, floor_code, floor_name, site_name, service_name in candidates:
        haystack = f'{service_code} {service_name} {floor_code} {floor_name}'.lower()
        if DMM_FLOOR.lower() in haystack:
            return site_code, service_code, floor_code

    print(f'❌ service/floor = "{DMM_FLOOR}" が見つかりませんでした。')
    all_site_names = sorted(set(f'{c[5]}({c[0]})' for c in candidates))
    print(f'   FloorListから取得できた site 一覧: {all_site_names}')
    print('   取得できた site/service/floor の一覧（全件）:')
    for site_code, service_code, floor_id, floor_code, floor_name, site_name, service_name in candidates:
        print(f'   - site={site_code}({site_name})  service={service_code}({service_name})  floor={floor_code}({floor_name})')
    sys.exit(1)


def fetch_ranking(hits=FETCH_HITS):
    """DMM ItemList APIから人気順（rank）の同人ランキングを取得する。"""
    site_code, service_code, floor_code = resolve_floor_code()
    print(f'ℹ️ 使用する site={site_code} / service={service_code} / floor={floor_code}')

    params = {
        'api_id': DMM_API_ID,
        'affiliate_id': DMM_AFFILIATE_ID,
        'site': site_code,
        'service': service_code,
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
        images.append(None)  # 画像なし（プレースホルダー）

    posts = []
    # 投稿1（メイン）：タイトル + 何番目の投稿か (1/5)
    posts.append({
        'label': '投稿1（メイン）',
        'text': f'{title}\n(1/5)',
        'image': images[0],
    })
    # リプライ2〜4：何番目の投稿か (n/5) のみ
    for i in range(1, 4):
        posts.append({
            'label': f'リプライ{i + 1}',
            'text': f'({i + 1}/5)',
            'image': images[i],
        })
    # リプライ5（アフィリエイトURL付き + (5/5)）
    posts.append({
        'label': 'リプライ5',
        'text': f'▼詳細・購入はこちら\n{affiliate_url}\n(5/5)',
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
        print(f'    画像: {p["image"] or "(サンプル画像なし)"}')


# ================================================================
# 🧵 Buffer連携（新API / GraphQL）
#    saveToDraft=false でBufferキューに登録し、自動公開する。
# ================================================================

def buffer_graphql_request(query, variables=None):
    """Buffer GraphQL APIへリクエストを送る共通関数。

    GraphQLの流儀上、HTTPレベルのエラー（認証エラー等）以外は常に200が返り、
    エラー情報は response['errors'] や MutationError として返ってくる点に注意。
    """
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {BUFFER_API_KEY}',
    }
    payload = {'query': query, 'variables': variables or {}}
    try:
        resp = requests.post(BUFFER_GRAPHQL_ENDPOINT, headers=headers, json=payload, timeout=30)
    except Exception as e:
        return False, f'Buffer APIへの接続に失敗しました: {e}'

    if resp.status_code != 200:
        return False, f'Buffer APIがHTTP {resp.status_code} を返しました: {resp.text[:500]}'

    try:
        data = resp.json()
    except Exception as e:
        return False, f'Buffer APIレスポンスのJSON解析に失敗しました: {e}'

    if 'errors' in data and data['errors']:
        # システムレベルのGraphQLエラー（認証エラー・レート制限等）
        codes = [err.get('extensions', {}).get('code') for err in data['errors']]
        messages = [err.get('message', '') for err in data['errors']]
        return False, f'Buffer APIエラー（{codes}）: {messages}'

    return True, data.get('data', {})


CREATE_THREADED_DRAFT_POST_MUTATION = """
mutation CreateThreadedDraftPost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess {
      post {
        id
        status
      }
    }
    ... on MutationError {
      message
    }
  }
}
"""


def _asset_input_for_image(image_url):
    """新Buffer API（[AssetInput!]形式）向けに画像1枚をassets要素に変換する。
    プレースホルダー（実URLでない）場合はNoneを返し、呼び出し側で除外する。
    """
    if image_url and image_url.startswith('http'):
        return {'image': {'url': image_url}}
    return None


def build_thread_metadata(posts):
    """postsのリストから、Buffer new API の metadata.twitter.thread 用の
    ThreadedPostInput配列を組み立てる。"""
    thread_items = []
    for post in posts:
        assets = []
        asset = _asset_input_for_image(post.get('image'))
        if asset:
            assets.append(asset)
        thread_items.append({
            'text': post['text'],
            'assets': assets,
        })
    return thread_items


def send_thread_to_buffer(rank, posts, channel_id):
    """スレッド1本分（5投稿）を、1回の createPost 呼び出しで
    Xの正式なリプライスレッドとしてBufferのキューに登録し、自動公開する。

    ※ Buffer new API は metadata.twitter.thread によりスレッドをネイティブに
       サポートしているため、5件バラバラの独立投稿にはならない。
    ※ saveToDraft=false のため、Buffer側の投稿スケジュールに従って
       人間の承認なしに自動的にXへ公開される。
    """
    thread_items = build_thread_metadata(posts)
    if not thread_items:
        return False, 'スレッド項目の組み立てに失敗しました。'

    # トップレベルの text / assets はスレッド先頭の投稿と一致させる仕様
    first_text = thread_items[0]['text']
    first_assets = thread_items[0]['assets']

    variables = {
        'input': {
            'text': first_text,
            'channelId': channel_id,
            'schedulingType': 'automatic',
            'mode': 'addToQueue',
            'saveToDraft': BUFFER_SAVE_AS_DRAFT,
            'assets': first_assets,
            'metadata': {
                'twitter': {
                    'thread': thread_items,
                }
            },
        }
    }

    print(f'\n📤 {rank}位（{len(thread_items)}投稿スレッド）をBufferに送信中（自動公開）...')
    ok, result = buffer_graphql_request(CREATE_THREADED_DRAFT_POST_MUTATION, variables)
    if not ok:
        print(f'   ❌ 送信失敗: {result}')
        return False, result

    create_result = result.get('createPost', {})
    if 'message' in create_result:
        # MutationError（Buffer側のバリデーションエラー等）
        print(f'   ❌ Buffer側でエラー: {create_result["message"]}')
        return False, create_result['message']

    post = create_result.get('post', {})
    print(f'   ✅ Bufferキュー登録成功: post_id={post.get("id")} status={post.get("status")}')
    return True, post


# ================================================================
# 🚀 メイン処理
# ================================================================

def main():
    history = load_history()
    used_ids = {h['content_id'] for h in history}

    print('=' * 60)
    print(f'📋 DMM同人ランキング投稿生成（対象: 上位{RANK_COUNT}件・重複除外）')
    print('=' * 60)

    if BUFFER_ENABLED:
        print(
            f'🧵 Buffer連携: 有効（新API/GraphQL、送信先チャンネル数: {len(BUFFER_CHANNEL_IDS)}、'
            f'saveToDraft={BUFFER_SAVE_AS_DRAFT}＝自動公開）'
        )
    else:
        print('🧵 Buffer連携: 無効（BUFFER_API_KEY / BUFFER_CHANNEL_IDS 未設定。コンソール出力のみ）')

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

        if BUFFER_ENABLED:
            for channel_id in BUFFER_CHANNEL_IDS:
                send_thread_to_buffer(display_rank, posts, channel_id)

        new_history_entries.append({
            'content_id': content_id,
            'title': item.get('title', ''),
            'date': datetime.date.today().isoformat(),
        })

    history.extend(new_history_entries)
    save_history(history)

    print('\n' + '=' * 60)
    print(f'✅ 完了: {len(picked)}件の投稿内容をコンソールに出力しました。')
    if BUFFER_ENABLED:
        print('   Bufferのキューに登録済みです。Buffer側の投稿スケジュールに従って')
        print('   自動的にXへ公開されます（人間の承認ステップはありません）。')
    else:
        print('   内容を確認のうえ、手動でXに投稿してください。')
    print(f'📚 履歴ファイル（重複判定用）: {HISTORY_FILE}')
    print('=' * 60)


if __name__ == '__main__':
    main()
