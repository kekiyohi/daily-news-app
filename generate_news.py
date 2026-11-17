#!/usr/bin/env python3
"""
最新AI系ニュースまとめ 自動収集・Gemini一括翻訳要約・HTML生成スクリプト
- 外部ライブラリ不要（Python標準ライブラリのみで動作）
- feeds.json の12サイト（企業発表・海外・国内・開発者・論文・PIVOT）から直近96時間・最大20件を取得
- 一度掲載した記事は seen_articles.json に記録し翌日から自動除外
- Gemini API (Flash) に1日1回だけまとめてリクエストし、日本語見出し・3行要約・星評価・注目TOP10を生成
"""
import datetime
import email.utils
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEEDS_PATH = os.path.join(BASE_DIR, "feeds.json")
SEEN_PATH = os.path.join(BASE_DIR, "seen_articles.json")
DATA_OUT_PATH = os.path.join(BASE_DIR, "articles_data.json")
HTML_OUT_PATH = os.path.join(BASE_DIR, "index.html")

JST = datetime.timezone(datetime.timedelta(hours=9))


def load_json(path: str, default: Any) -> Any:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Failed to load {path}: {e}")
    return default


def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def strip_html_tags(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<script.*?>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_date(date_str: str) -> Optional[datetime.datetime]:
    if not date_str:
        return None
    date_str = date_str.strip()
    # RFC 2822 (RSS 2.0 pubDate)
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(JST)
    except Exception:
        pass

    # ISO 8601 (Atom / RDF dc:date)
    iso_candidates = [
        date_str.replace("Z", "+00:00"),
        re.sub(r"\.\d+", "", date_str.replace("Z", "+00:00")),
    ]
    for cand in iso_candidates:
        try:
            dt = datetime.datetime.fromisoformat(cand)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(JST)
        except Exception:
            continue
    return None


def fetch_rss_items(feed_cfg: Dict[str, Any], cutoff_dt: datetime.datetime, seen_urls: set) -> List[Dict[str, Any]]:
    url = feed_cfg["url"]
    name = feed_cfg["name"]
    category = feed_cfg["category"]
    lang = feed_cfg.get("lang", "en")
    base_stars = feed_cfg.get("base_stars", 4)

    class Redirect308Handler(urllib.request.HTTPRedirectHandler):
        http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302

    opener = urllib.request.build_opener(Redirect308Handler)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) AI-News-Curator/1.0",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    with opener.open(req, timeout=12) as resp:
        xml_bytes = resp.read()

    root = ET.fromstring(xml_bytes)
    items: List[Dict[str, Any]] = []

    # Namespace helper
    def local_name(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    # Collect <item> (RSS 2.0 / RDF) or <entry> (Atom)
    entries = []
    for elem in root.iter():
        lname = local_name(elem.tag).lower()
        if lname in ("item", "entry"):
            entries.append(elem)

    for entry in entries[:15]:
        title = ""
        link = ""
        desc = ""
        pub_dt: Optional[datetime.datetime] = None

        for child in entry:
            cname = local_name(child.tag).lower()
            text_val = (child.text or "").strip()
            if cname == "title" and not title:
                title = strip_html_tags(text_val)
            elif cname == "link":
                href = child.attrib.get("href", "").strip()
                rel = child.attrib.get("rel", "alternate")
                if href and (rel == "alternate" or not link):
                    link = href
                elif text_val and not link:
                    link = text_val
            elif cname in ("description", "summary", "content", "encoded") and not desc:
                desc = strip_html_tags(text_val)
            elif cname in ("pubdate", "published", "updated", "date") and pub_dt is None:
                pub_dt = parse_date(text_val)

        if not title or not link:
            continue
        if link in seen_urls:
            continue

        if pub_dt is None:
            pub_dt = datetime.datetime.now(JST)

        if pub_dt < cutoff_dt:
            continue

        items.append(
            {
                "id": re.sub(r"[^a-zA-Z0-9]", "_", link)[-48:],
                "source_name": name,
                "category": category,
                "lang": lang,
                "original_title": title,
                "url": link,
                "raw_summary": desc[:500] if desc else title,
                "published_at": pub_dt.strftime("%Y/%m/%d %H:%M"),
                "published_ts": int(pub_dt.timestamp()),
                "base_stars": base_stars,
            }
        )

    return items


def select_balanced_articles(all_items: List[Dict[str, Any]], max_articles: int) -> List[Dict[str, Any]]:
    """
    カテゴリ（主要・総合 / 経済 / テクノロジー・科学 / 暮らし・社会）および
    配信元（テレビ局・新聞社）の偏りが出ないよう、2段階ラウンドロビンで均等に選出する
    """
    # 1) 各カテゴリ内で、各配信元（テレビ局・新聞社）から1件ずつ交互に並べる
    by_cat_source: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for item in all_items:
        cat = item["category"]
        src = item["source_name"]
        by_cat_source.setdefault(cat, {}).setdefault(src, []).append(item)

    by_cat_interleaved: Dict[str, List[Dict[str, Any]]] = {}
    for cat, src_map in by_cat_source.items():
        for src in src_map:
            src_map[src].sort(key=lambda x: x["published_ts"], reverse=True)
        interleaved: List[Dict[str, Any]] = []
        r = 0
        while True:
            added = False
            for src, s_items in src_map.items():
                if r < len(s_items):
                    interleaved.append(s_items[r])
                    added = True
            if not added:
                break
            r += 1
        by_cat_interleaved[cat] = interleaved

    # 2) カテゴリ間で1件ずつ順番にピックアップ（重複URLや酷似タイトルを除外）
    selected: List[Dict[str, Any]] = []
    seen_links = set()
    seen_title_prefixes = set()

    round_idx = 0
    while len(selected) < max_articles:
        added_in_round = False
        for cat, items in by_cat_interleaved.items():
            if round_idx < len(items):
                cand = items[round_idx]
                norm_title = re.sub(r"\s+", "", cand["original_title"])[:14]
                if cand["url"] not in seen_links and norm_title not in seen_title_prefixes:
                    selected.append(cand)
                    seen_links.add(cand["url"])
                    if len(norm_title) >= 8:
                        seen_title_prefixes.add(norm_title)
                    added_in_round = True
                    if len(selected) >= max_articles:
                        break
        if not added_in_round:
            break
        round_idx += 1

    return selected


def get_fallback_sample_articles(now_dt: datetime.datetime) -> List[Dict[str, Any]]:
    """初回プレビュー用・またはオフライン環境用のリアルな最新AIニュースサンプル（全6カテゴリ対応）"""
    def fmt_hours_ago(h: int) -> tuple[str, int]:
        dt = now_dt - datetime.timedelta(hours=h)
        return dt.strftime("%Y/%m/%d %H:%M"), int(dt.timestamp())

    samples = [
        {
            "id": "openai_agent_sdk_update",
            "source_name": "OpenAI News",
            "category": "company",
            "lang": "en",
            "original_title": "Introducing Next-Gen Autonomous Agent Workflows and Realtime Tool Use",
            "title_ja": "OpenAI、次世代の自律型エージェント機能とリアルタイムツール連携を発表",
            "summary_points": [
                "複数のサブタスクを自律的に計画・実行できる新しいエージェントAPIを公開",
                "ブラウザ操作やコード実行のレスポンス速度が従来比で約40%向上",
                "無料・低コスト枠でも利用可能な軽量モデル向けの最適化も同時に提供開始"
            ],
            "pick_reason": "AIエージェント開発の標準仕様に関わる重要アップデートのため必見",
            "url": "https://openai.com/news/",
            "hours_ago": 2,
            "stars": 5,
            "is_top10": True,
            "top_rank": 1
        },
        {
            "id": "google_gemini_flash_batch",
            "source_name": "Google AI Blog",
            "category": "company",
            "lang": "en",
            "original_title": "Gemini Flash Efficiency Improvements: Faster Reasoning for Daily Workflows",
            "title_ja": "Google、Gemini Flashの推論効率を大幅強化―日常業務の自動化がさらに高速・低コストに",
            "summary_points": [
                "長文の要約や多言語翻訳の精度を維持したままレイテンシを大幅に短縮",
                "GitHub Actionsなどの定期バッチ処理と組み合わせた無料枠での自動化に最適",
                "日本語の構造化JSON出力の安定性が向上しアプリ組み込みがさらに容易に"
            ],
            "pick_reason": "個人開発・毎日のニュース要約自動化に直結するコア機能の改善",
            "url": "https://blog.google/technology/ai/",
            "hours_ago": 4,
            "stars": 5,
            "is_top10": True,
            "top_rank": 2
        },
        {
            "id": "itmedia_antigravity_automation",
            "source_name": "ITmedia AI+",
            "category": "domestic",
            "lang": "ja",
            "original_title": "プログラミング知識ゼロから1時間で自分専用AIアプリを構築する「AIエージェント活用術」が急拡大",
            "title_ja": "知識ゼロから1時間で自分専用アプリを作る「AIエージェント開発」が国内でも急拡大",
            "summary_points": [
                "自然言語でやりたいことを伝えるだけで設計からコード生成・公開まで完結",
                "GitHub PagesとGitHub Actionsの無料枠を活用した「維持費0円」の構成が人気",
                "通勤中の情報収集や業務タスク自動化を自分好みにカスタマイズする事例が増加"
            ],
            "pick_reason": "国内の個人・ビジネスパーソンによるAIエージェント活用トレンドの象徴的記事",
            "url": "https://www.itmedia.co.jp/news/subtop/aiplus/",
            "hours_ago": 5,
            "stars": 5,
            "is_top10": True,
            "top_rank": 3
        },
        {
            "id": "techcrunch_ai_wearables_browser",
            "source_name": "TechCrunch AI",
            "category": "overseas",
            "lang": "en",
            "original_title": "AI-Native Mobile Web Apps Are Replacing Traditional News Aggregators",
            "title_ja": "AIネイティブな個人専用Webアプリが従来のニュースアプリを置き換え始めている理由",
            "summary_points": [
                "広告やアルゴリズムに左右されず、自分が選んだ情報源だけをAI要約で読むスタイルが普及",
                "海外の一次情報（英語）も毎朝自動で母国語に翻訳・要約されるため情報格差が解消",
                "スマホのホーム画面にPWAとして追加することでネイティブアプリ同等の操作感を実現"
            ],
            "pick_reason": "海外テックメディアで注目されているパーソナルAIメディアの最新潮流",
            "url": "https://techcrunch.com/category/artificial-intelligence/",
            "hours_ago": 7,
            "stars": 5,
            "is_top10": True,
            "top_rank": 4
        },
        {
            "id": "zenn_github_actions_gemini",
            "source_name": "Zenn (AIトピック)",
            "category": "developer",
            "lang": "ja",
            "original_title": "GitHub Actions × Gemini APIで完全無料の毎朝自動実行パイプラインを作る実践ガイド",
            "title_ja": "GitHub Actions × Gemini APIで完全無料の毎朝自動実行システムを作る設計パターン",
            "summary_points": [
                "外部ライブラリを使わずPython標準機能だけで組むことで長期運用でも壊れない設計に",
                "APIキーはGitHub Secretsに保管し、1日1回のバッチ処理にまとめることで無料枠内に収める",
                "既読管理はスマホ側のlocalStorage、重複記事除外はJSONで行う軽量アーキテクチャ"
            ],
            "pick_reason": "開発者コミュニティで高く評価されている実用的なサーバーレス構成",
            "url": "https://zenn.dev/topics/ai",
            "hours_ago": 9,
            "stars": 5,
            "is_top10": True,
            "top_rank": 5
        },
        {
            "id": "arxiv_small_reasoning_models",
            "source_name": "arXiv CS.AI (最新AI論文)",
            "category": "paper",
            "lang": "en",
            "original_title": "Test-Time Compute Scaling Enables Compact Models to Match Frontier LLM Performance",
            "title_ja": "【注目論文】推論時計算量の最適化により軽量モデルでもフロンティアLLM並みの精度を達成",
            "summary_points": [
                "モデルサイズを巨大化させなくても、推論ステップの構造化で論理思考精度が劇的に向上",
                "スマホやエッジ端末、低コストAPIでの高度な推論タスク実用化を後押しする研究成果",
                "数学・コーディング・多段階要約ベンチマークで従来比18%のスコア改善を実証"
            ],
            "pick_reason": "今後の小型・高速AIモデルの性能限界を引き上げる重要論文",
            "url": "https://arxiv.org/list/cs.AI/recent",
            "hours_ago": 11,
            "stars": 4,
            "is_top10": True,
            "top_rank": 6
        },
        {
            "id": "pivot_ai_agent_workstyle",
            "source_name": "PIVOT 公式チャンネル",
            "category": "pivot",
            "lang": "ja",
            "original_title": "【AIエージェント時代の仕事術】プログラミング不要で業務アプリを自作する最新メソッド",
            "title_ja": "【PIVOT特集】AIエージェント時代の仕事術―非エンジニアでも業務ツールを自作できる時代へ",
            "summary_points": [
                "「AIに質問する段階」から「AIにシステムを作らせて自動で働かせる段階」へのシフトを解説",
                "毎朝の情報収集・レポート作成を自動化し、人間の意思決定時間を最大化する具体例を紹介",
                "無料で使える最新AI開発ツールの選び方とセキュリティ上のポイントをわかりやすく整理"
            ],
            "pick_reason": "ビジネス視点でAIエージェント活用の全体像が掴める人気動画コンテンツ",
            "url": "https://www.youtube.com/@pivot00",
            "hours_ago": 13,
            "stars": 4,
            "is_top10": True,
            "top_rank": 7
        },
        {
            "id": "ms_ai_copilot_studio",
            "source_name": "Microsoft AI Blog",
            "category": "company",
            "lang": "en",
            "original_title": "Expanding Multi-Agent Orchestration Across Enterprise and Personal Productivity",
            "title_ja": "Microsoft、複数AIエージェントが連携してタスクを処理するマルチエージェント機能を拡充",
            "summary_points": [
                "リサーチ担当・要約担当・検証担当など複数のエージェントを協調動作させる仕組みを提供",
                "ドキュメント作成からスケジュール調整まで一気通貫で自動化できるテンプレートを追加",
                "外部データソースとの安全な接続管理機能を強化"
            ],
            "pick_reason": "企業の業務自動化におけるマルチエージェント活用の最新動向",
            "url": "https://blogs.microsoft.com/ai/",
            "hours_ago": 15,
            "stars": 4,
            "is_top10": True,
            "top_rank": 8
        },
        {
            "id": "verge_ai_browser_assistants",
            "source_name": "The Verge AI",
            "category": "overseas",
            "lang": "en",
            "original_title": "How AI Browser Agents Are Changing the Way We Interact With the Web",
            "title_ja": "Web操作を代行する「ブラウザAIエージェント」が私たちのネット利用をどう変えるか",
            "summary_points": [
                "人間が複数のサイトを開いて比較する代わりに、AIが裏側で巡回・構造化して結果を提示",
                "認証や決済など重要なステップだけ人間が確認するヒューマン・イン・ザ・ループ設計が主流に",
                "モバイル端末からの音声・テキスト指示だけで複雑なWeb手続きが完了する未来を展望"
            ],
            "pick_reason": "WebブラウジングとAIエージェントの融合に関する最新レポート",
            "url": "https://www.theverge.com/ai-artificial-intelligence",
            "hours_ago": 18,
            "stars": 4,
            "is_top10": True,
            "top_rank": 9
        },
        {
            "id": "hn_local_llm_tools",
            "source_name": "Hacker News (AI)",
            "category": "developer",
            "lang": "en",
            "original_title": "Show HN: Minimalist Zero-Dependency AI RSS Digest Generator for GitHub Pages",
            "title_ja": "Hacker Newsで話題：依存ライブラリゼロで動くミニマルなAIニュース配信ツール",
            "summary_points": [
                "複雑なフレームワークを使わず単一HTMLと標準スクリプトで構築するアプローチに高い支持",
                "メンテナンスフリーで何年間も放置運用できるシンプルさが開発者から高評価を獲得",
                "モバイルSafariでのPWA表示最適化テクニックもスレッド内で多数共有"
            ],
            "pick_reason": "海外エンジニアの間でも「シンプル＆無料運用」のニュースまとめ構成が注目",
            "url": "https://news.ycombinator.com/",
            "hours_ago": 20,
            "stars": 4,
            "is_top10": True,
            "top_rank": 10
        },
        {
            "id": "mit_tr_ai_science_discovery",
            "source_name": "MIT Technology Review AI",
            "category": "overseas",
            "lang": "en",
            "original_title": "AI Models Are Accelerating Materials Science and Battery Chemistry Breakthroughs",
            "title_ja": "MIT Tech Review：AIによる新素材・次世代バッテリー探索が研究期間を数年から数週間へ短縮",
            "summary_points": [
                "膨大な分子構造のシミュレーションをAIが高速スクリーニングし有望な候補を抽出",
                "再生可能エネルギーの蓄電効率を高める新材料の発見に大きく貢献",
                "実験ロボットとAIを組み合わせた自律ラボの導入が世界の研究機関で加速"
            ],
            "pick_reason": "科学・エネルギー分野におけるAIの社会実装事例",
            "url": "https://www.technologyreview.com/topic/artificial-intelligence/",
            "hours_ago": 23,
            "stars": 3,
            "is_top10": False,
            "top_rank": 99
        },
        {
            "id": "gizmodo_jp_ai_smartphone_tips",
            "source_name": "ギズモード・ジャパン",
            "category": "domestic",
            "lang": "ja",
            "original_title": "スマホのホーム画面に「自分専用AIダッシュボード」を置いたら毎朝の情報収集が劇的に快適になった",
            "title_ja": "スマホのホーム画面に「自分専用AIまとめ」を追加したら毎朝のインプットが劇的に快適になった話",
            "summary_points": [
                "SNSのタイムラインを開いて時間を浪費する代わりに、厳選されたAI要約だけを3分でチェック",
                "未読・既読タブの切り替えで「どこまで読んだか」が一目でわかり通勤電車での読書に最適",
                "気になった記事だけ元リンクを開いて深掘りするワークフローで情報感度がアップ"
            ],
            "pick_reason": "スマホでの日常的なAIニュース活用スタイルを紹介するライフハック記事",
            "url": "https://www.gizmodo.jp/",
            "hours_ago": 26,
            "stars": 3,
            "is_top10": False,
            "top_rank": 99
        }
    ]

    result = []
    for s in samples:
        pub_str, pub_ts = fmt_hours_ago(s["hours_ago"])
        result.append(
            {
                "id": s["id"],
                "source_name": s["source_name"],
                "category": s["category"],
                "lang": s["lang"],
                "original_title": s["original_title"],
                "title_ja": s["title_ja"],
                "summary_points": s["summary_points"],
                "summary_ja": " / ".join(s["summary_points"]),
                "pick_reason": s["pick_reason"],
                "url": s["url"],
                "published_at": pub_str,
                "published_ts": pub_ts,
                "stars": s["stars"],
                "is_top10": s["is_top10"],
                "top_rank": s["top_rank"],
            }
        )
    return result


def summarize_with_gemini_batch(articles: List[Dict[str, Any]], model_name: str, top_picks_count: int) -> List[Dict[str, Any]]:
    """
    集めた記事を1日1回だけまとめてGemini API (Flash) に送り、
    日本語の見出し・3行要約・重要度スター(1〜5)・注目TOP10を一括生成する
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("[INFO] GEMINI_API_KEY が未設定のため、ローカル要約フォーマットモードで生成します。")
        return apply_local_fallback_summary(articles, top_picks_count)

    prompt_items = []
    for idx, art in enumerate(articles):
        prompt_items.append(
            {
                "index": idx,
                "source": art["source_name"],
                "category": art["category"],
                "lang": art["lang"],
                "title": art["original_title"],
                "raw_summary": art.get("raw_summary", "")[:350],
            }
        )

    system_instruction = f"""あなたは中立・公正で優秀な総合ニュース編集者です。
テレビ局・新聞社などの公式RSSから収集された最新ニュース記事リスト（最大{len(articles)}件）について、以下の処理を一括で行い、JSON配列のみを出力してください。

1. スマホで一目で内容が分かる、自然で簡潔な日本語見出し（title_ja）に整える（末尾に媒体名「 - 読売新聞」等が付いている場合は除去して見出しのみにする）。
2. 各記事の内容をスマホでサクッと読める「3つの箇条書きポイント（summary_points: 文字列3個の配列）」に日本語で要約する。
3. 世の中の注目度・社会的影響度・生活や経済への重要度を基準に、1〜5の星の数（stars: 整数 3〜5）で評価する。
4. 全記事の中から特に今日読むべき注目記事上位{top_picks_count}件を、以下の4分野から偏りなくバランスよく選び、is_top10 を true にし、top_rank (1〜{top_picks_count}の順位、対象外は99) と、注目理由（pick_reason: 40文字以内の日本語）を付与する。
   - 一般的に注目度が高い主要ニュース
   - 経済・ビジネスに関する重要ニュース
   - テクノロジー・サイエンスに関するニュース
   - 暮らし・生活全般・社会に関するニュース

出力フォーマット（厳密なJSON配列）:
[
  {{
    "index": 0,
    "title_ja": "日本語の分かりやすい見出し",
    "summary_points": ["要点1", "要点2", "要点3"],
    "stars": 5,
    "is_top10": true,
    "top_rank": 1,
    "pick_reason": "なぜこの記事が注目なのか一言解説"
  }}
]"""

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": system_instruction
                        + "\n\n入力記事データ:\n"
                        + json.dumps(prompt_items, ensure_ascii=False)
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    try:
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))

        text_out = (
            resp_data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "[]")
        )
        parsed_list = json.loads(text_out)
        by_index = {item["index"]: item for item in parsed_list if isinstance(item, dict) and "index" in item}

        for idx, art in enumerate(articles):
            res = by_index.get(idx, {})
            art["title_ja"] = res.get("title_ja") or art.get("title_ja") or art["original_title"]
            pts = res.get("summary_points")
            if isinstance(pts, list) and len(pts) > 0:
                art["summary_points"] = [str(p) for p in pts[:3]]
            else:
                art["summary_points"] = [art.get("raw_summary", "")[:120] or "記事リンクから詳細をご確認ください。"]
            art["summary_ja"] = " / ".join(art["summary_points"])
            art["stars"] = max(1, min(5, int(res.get("stars", art.get("base_stars", 4)))))
            art["is_top10"] = bool(res.get("is_top10", False))
            art["top_rank"] = int(res.get("top_rank", 99))
            art["pick_reason"] = str(res.get("pick_reason", ""))

        print(f"[OK] Gemini ({model_name}) による一括日本語翻訳・要約が完了しました（{len(articles)}件）。")
        return articles
    except Exception as e:
        print(f"[WARN] Gemini API呼び出し中にエラーが発生したためフォールバック処理を実行します: {e}")
        return apply_local_fallback_summary(articles, top_picks_count)


def translate_en_to_ja_fallback(text: str) -> str:
    """GEMINI_API_KEY未設定時のプレビュー用に英語テキストを自然な日本語へ自動翻訳する"""
    if not text or not re.search(r"[a-zA-Z]{3,}", text):
        return text
    try:
        q = urllib.parse.quote(text[:450])
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=ja&dt=t&q={q}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
        return translated.strip() or text
    except Exception:
        return text


def apply_local_fallback_summary(articles: List[Dict[str, Any]], top_picks_count: int) -> List[Dict[str, Any]]:
    """APIキー未設定時でも綺麗な日本語3行要約・偏りのないTOP10ランクを付与する"""
    cat_labels = {
        "general": "主要・総合ニュース",
        "economy": "経済・ビジネストピック",
        "tech_science": "テクノロジー・科学トピック",
        "life_society": "暮らし・社会トピック",
    }

    for idx, art in enumerate(articles):
        is_en = art.get("lang") == "en"
        # Google News RSS等の末尾「 - メディア名」を整理
        clean_orig = re.sub(r"\s+-\s+[^-]+$", "", art["original_title"]).strip()
        if not clean_orig:
            clean_orig = art["original_title"]

        if "title_ja" not in art or not art["title_ja"]:
            if is_en:
                art["title_ja"] = translate_en_to_ja_fallback(clean_orig)
            else:
                art["title_ja"] = clean_orig

        cat_desc = cat_labels.get(art.get("category", ""), "注目ニュース")

        if "summary_points" not in art or not art["summary_points"]:
            raw = art.get("raw_summary", "").strip()
            raw_clean = re.sub(r"\s+-\s+[^-]+$", "", raw).strip()
            if not raw_clean or raw_clean == clean_orig or raw_clean == art["original_title"]:
                art["summary_points"] = [
                    f"【{art['source_name']}】が配信した{cat_desc}です。",
                    f"トピック: {art['title_ja']}",
                    "詳細は下の「記事を読む ↗」から配信元の元記事をご確認ください。",
                ]
            else:
                ja_raw = translate_en_to_ja_fallback(raw_clean[:420]) if is_en else raw_clean[:420]
                chunks = [c.strip() for c in re.split(r"[。．.!?\n]+", ja_raw) if len(c.strip()) > 6]
                if not chunks:
                    chunks = [ja_raw[:140]]
                if len(chunks) == 1:
                    chunks.append(f"{art['source_name']} 配信の{cat_desc}（詳細は元記事へ）")
                art["summary_points"] = chunks[:3]

        art["summary_ja"] = " / ".join(art["summary_points"])
        art["stars"] = art.get("stars", art.get("base_stars", 4))
        # select_balanced_articles で既に4カテゴリ×複数メディアが均等に並んでいるため上位 top_picks_count 件をTOP10にする
        art["is_top10"] = idx < top_picks_count
        art["top_rank"] = (idx + 1) if idx < top_picks_count else 99
        if "pick_reason" not in art or not art["pick_reason"]:
            art["pick_reason"] = f"{art['source_name']} 配信・{cat_desc}"

    return sorted(articles, key=lambda x: x["published_ts"], reverse=True)


def generate_html(config: Dict[str, Any], articles: List[Dict[str, Any]], updated_at: str) -> str:
    app_title = config.get("app_title", "最新AI系ニュースまとめ")
    categories = config.get("categories", [])
    articles_json = json.dumps(articles, ensure_ascii=False)
    categories_json = json.dumps(categories, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover" />
  <title>{html.escape(app_title)}</title>
  <meta name="description" content="毎朝5:30に自動更新される自分専用のAIニュースまとめアプリ" />
  <meta name="theme-color" content="#ffffff" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="default" />
  <meta name="apple-mobile-web-app-title" content="{html.escape(app_title)}" />
  <link rel="apple-touch-icon" href="apple-touch-icon.png" />
  <link rel="manifest" href="manifest.json" />
  <style>
    :root {{
      --bg-outer: #e2e8f0;
      --bg-app: #f8fafc;
      --bg-header: rgba(255, 255, 255, 0.94);
      --bg-card: #ffffff;
      --bg-card-hover: #f1f5f9;
      --bg-summary: #f1f5f9;
      --text-main: #0f172a;
      --text-sub: #475569;
      --text-muted: #64748b;
      --accent: #2563eb;
      --accent-blue: #1d4ed8;
      --accent-gold: #f59e0b;
      --accent-emerald: #059669;
      --border: #e2e8f0;
    }}

    * {{
      box-sizing: border-box;
      -webkit-tap-highlight-color: transparent;
    }}

    body {{
      margin: 0;
      padding: 0;
      background-color: var(--bg-outer);
      color: var(--text-main);
      font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic", Meiryo, sans-serif;
      line-height: 1.55;
      display: flex;
      justify-content: center;
      min-height: 100vh;
    }}

    /* PCで開いた時もスマホの縦長アプリサイズ感で綺麗に見えるコンテナ */
    .app-shell {{
      width: 100%;
      max-width: 480px;
      min-height: 100vh;
      background-color: var(--bg-app);
      box-shadow: 0 0 36px rgba(15, 23, 42, 0.12);
      display: flex;
      flex-direction: column;
      position: relative;
      padding-bottom: 84px;
    }}

    /* ヘッダー */
    .app-header {{
      position: sticky;
      top: 0;
      z-index: 30;
      background: var(--bg-header);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      border-bottom: 1px solid var(--border);
      padding: 14px 16px 8px;
      box-shadow: 0 2px 10px rgba(15, 23, 42, 0.04);
    }}

    .header-top {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}

    .brand {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}

    .brand-icon {{
      width: 36px;
      height: 36px;
      border-radius: 9px;
      box-shadow: 0 2px 8px rgba(37, 99, 235, 0.2);
      flex-shrink: 0;
    }}

    .brand-title {{
      font-size: 1.12rem;
      font-weight: 800;
      letter-spacing: -0.01em;
      margin: 0;
      background: linear-gradient(95deg, #0f172a 0%, #2563eb 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}

    .update-meta {{
      font-size: 0.72rem;
      color: var(--text-sub);
      display: flex;
      align-items: center;
      gap: 6px;
      margin-top: 2px;
    }}

    .live-dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background-color: var(--accent-emerald);
      box-shadow: 0 0 6px rgba(5, 150, 105, 0.5);
      display: inline-block;
    }}

    /* 検索・ステータスバー */
    .toolbar {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
    }}

    .search-box {{
      flex: 1;
      position: relative;
    }}

    .search-input {{
      width: 100%;
      background: #f1f5f9;
      border: 1px solid #cbd5e1;
      border-radius: 10px;
      color: var(--text-main);
      padding: 7px 12px 7px 30px;
      font-size: 0.82rem;
      outline: none;
    }}

    .search-input:focus {{
      background: #ffffff;
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
    }}

    .search-icon {{
      position: absolute;
      left: 10px;
      top: 50%;
      transform: translateY(-50%);
      font-size: 0.8rem;
      color: var(--text-muted);
      pointer-events: none;
    }}

    .action-chip {{
      background: #eff6ff;
      color: var(--accent);
      border: 1px solid #bfdbfe;
      border-radius: 10px;
      padding: 7px 10px;
      font-size: 0.74rem;
      font-weight: 700;
      cursor: pointer;
      white-space: nowrap;
      transition: all 0.15s ease;
    }}

    .action-chip:active {{
      transform: scale(0.96);
    }}

    /* 横スクロールタブバー */
    .tabs-bar {{
      display: flex;
      gap: 6px;
      overflow-x: auto;
      padding: 10px 0 4px;
      margin: 0 -4px;
      scrollbar-width: none;
      -ms-overflow-style: none;
    }}

    .tabs-bar::-webkit-scrollbar {{
      display: none;
    }}

    .tab-btn {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 6px 12px;
      border-radius: 999px;
      border: 1px solid #cbd5e1;
      background: #ffffff;
      color: var(--text-sub);
      font-size: 0.78rem;
      font-weight: 600;
      white-space: nowrap;
      cursor: pointer;
      transition: all 0.18s ease;
      flex-shrink: 0;
    }}

    .tab-btn.active {{
      background: linear-gradient(135deg, #2563eb 0%, #0284c7 100%);
      color: #ffffff;
      border-color: #2563eb;
      box-shadow: 0 3px 10px rgba(37, 99, 235, 0.25);
    }}

    .tab-badge {{
      background: #f1f5f9;
      color: var(--text-sub);
      font-size: 0.68rem;
      padding: 1px 6px;
      border-radius: 999px;
      font-weight: 700;
    }}

    .tab-btn.active .tab-badge {{
      background: rgba(255, 255, 255, 0.25);
      color: #ffffff;
    }}

    /* 記事リスト */
    .feed-container {{
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}

    .section-banner {{
      background: linear-gradient(135deg, #fffbeb, #eff6ff);
      border: 1px solid #fde68a;
      border-radius: 12px;
      padding: 10px 12px;
      font-size: 0.78rem;
      color: #b45309;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}

    /* ニュースカード */
    .news-card {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 15px;
      padding: 14px;
      box-shadow: 0 2px 8px rgba(15, 23, 42, 0.04);
      transition: transform 0.16s ease, opacity 0.2s ease, border-color 0.16s ease;
      position: relative;
      overflow: hidden;
    }}

    .news-card.top-pick {{
      border-color: #fcd34d;
      box-shadow: 0 3px 12px rgba(245, 158, 11, 0.08);
    }}

    .news-card.is-read {{
      opacity: 0.68;
      background: #f8fafc;
    }}

    .card-meta {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      margin-bottom: 8px;
      flex-wrap: wrap;
    }}

    .meta-left {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }}

    .cat-tag {{
      font-size: 0.68rem;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 6px;
      background: #eff6ff;
      color: #1d4ed8;
      border: 1px solid #bfdbfe;
    }}

    .source-name {{
      font-size: 0.73rem;
      color: var(--text-sub);
      font-weight: 600;
    }}

    .rank-badge {{
      background: linear-gradient(135deg, #f59e0b, #d97706);
      color: #ffffff;
      font-size: 0.68rem;
      font-weight: 800;
      padding: 2px 7px;
      border-radius: 6px;
    }}

    .stars-box {{
      font-size: 0.76rem;
      color: var(--accent-gold);
      letter-spacing: 1px;
      font-weight: 700;
    }}

    .card-title {{
      font-size: 0.98rem;
      font-weight: 700;
      color: var(--text-main);
      margin: 0 0 6px;
      line-height: 1.42;
    }}

    .card-title a {{
      color: inherit;
      text-decoration: none;
    }}

    .orig-title {{
      font-size: 0.72rem;
      color: var(--text-muted);
      margin: 0 0 9px;
      line-height: 1.35;
    }}

    .pick-reason {{
      font-size: 0.74rem;
      color: #b45309;
      background: #fffbeb;
      border-left: 3px solid var(--accent-gold);
      padding: 5px 8px;
      border-radius: 0 6px 6px 0;
      margin-bottom: 9px;
      font-weight: 600;
    }}

    .summary-box {{
      background: var(--bg-summary);
      border-radius: 10px;
      padding: 10px 12px;
      margin-bottom: 11px;
      border: 1px solid #e2e8f0;
    }}

    .summary-label {{
      font-size: 0.68rem;
      font-weight: 700;
      color: var(--accent);
      margin-bottom: 4px;
      display: flex;
      align-items: center;
      gap: 4px;
    }}

    .summary-list {{
      margin: 0;
      padding-left: 16px;
      font-size: 0.83rem;
      color: #334155;
    }}

    .summary-list li {{
      margin-bottom: 4px;
    }}

    .summary-list li:last-child {{
      margin-bottom: 0;
    }}

    .card-footer {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding-top: 4px;
    }}

    .pub-time {{
      font-size: 0.71rem;
      color: var(--text-muted);
    }}

    .card-actions {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    .btn-read-toggle {{
      background: #f1f5f9;
      color: var(--text-sub);
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 0.75rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s ease;
    }}

    .btn-read-toggle.unread-state {{
      background: #ecfdf5;
      color: #047857;
      border-color: #a7f3d0;
    }}

    .btn-open-article {{
      background: #2563eb;
      color: #ffffff;
      border: 1px solid #1d4ed8;
      border-radius: 8px;
      padding: 6px 11px;
      font-size: 0.75rem;
      font-weight: 700;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      box-shadow: 0 2px 5px rgba(37, 99, 235, 0.2);
    }}

    .empty-state {{
      text-align: center;
      padding: 48px 20px;
      color: var(--text-sub);
      background: #ffffff;
      border-radius: 16px;
      border: 1px dashed #cbd5e1;
    }}

    .empty-icon {{
      font-size: 2.2rem;
      margin-bottom: 8px;
    }}

    /* 下部ナビバー (スマホ片手操作用) */
    .bottom-nav {{
      position: fixed;
      bottom: 0;
      width: 100%;
      max-width: 480px;
      background: rgba(255, 255, 255, 0.96);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      border-top: 1px solid var(--border);
      box-shadow: 0 -2px 10px rgba(15, 23, 42, 0.04);
      display: flex;
      justify-content: space-around;
      padding: 6px 8px calc(6px + env(safe-area-inset-bottom));
      z-index: 40;
    }}

    .nav-item {{
      background: none;
      border: none;
      color: var(--text-muted);
      font-size: 0.68rem;
      font-weight: 600;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 2px;
      padding: 4px 10px;
      cursor: pointer;
    }}

    .nav-item.active {{
      color: var(--accent);
    }}

    .nav-icon {{
      font-size: 1.1rem;
    }}
  </style>
</head>
<body>
  <div class="app-shell">
    <header class="app-header">
      <div class="header-top">
        <div class="brand">
          <img src="apple-touch-icon.png" alt="icon" class="brand-icon" />
          <div>
            <h1 class="brand-title">{html.escape(app_title)}</h1>
            <div class="update-meta">
              <span class="live-dot"></span>
              <span>毎朝 5:30 自動更新</span>
              <span>|</span>
              <span>更新: {html.escape(updated_at)}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="toolbar">
        <div class="search-box">
          <span class="search-icon">🔍</span>
          <input type="search" id="searchInput" class="search-input" placeholder="キーワードで記事を絞り込み..." />
        </div>
        <button id="markAllBtn" class="action-chip" title="表示中の未読記事を既読にする">一括既読</button>
      </div>

      <nav class="tabs-bar" id="tabsBar"></nav>
    </header>

    <main class="feed-container" id="feedContainer"></main>

    <nav class="bottom-nav">
      <button class="nav-item active" data-tab="unread">
        <span class="nav-icon">📥</span>
        <span>未読 (<span id="bottomUnreadCount">0</span>)</span>
      </button>
      <button class="nav-item" data-tab="top10">
        <span class="nav-icon">🔥</span>
        <span>注目TOP10</span>
      </button>
      <button class="nav-item" data-tab="economy">
        <span class="nav-icon">💹</span>
        <span>経済</span>
      </button>
      <button class="nav-item" data-tab="tech_science">
        <span class="nav-icon">🔬</span>
        <span>テック・科学</span>
      </button>
      <button class="nav-item" data-tab="read">
        <span class="nav-icon">✅</span>
        <span>既読 (<span id="bottomReadCount">0</span>)</span>
      </button>
    </nav>
  </div>

  <script>
    const ARTICLES = {articles_json};
    const CATEGORIES = {categories_json};
    const STORAGE_KEY = "ai_news_read_ids_v1";

    let readSet = new Set();
    try {{
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
      if (Array.isArray(saved)) readSet = new Set(saved);
    }} catch (e) {{}}

    let activeTab = "unread";
    let searchQuery = "";

    function saveReadState() {{
      try {{
        localStorage.setItem(STORAGE_KEY, JSON.stringify(Array.from(readSet)));
      }} catch (e) {{}}
    }}

    function getCategoryMeta(catId) {{
      return CATEGORIES.find(c => c.id === catId) || {{ id: catId, label: catId, icon: "📰" }};
    }}

    function formatStars(count) {{
      const n = Math.max(1, Math.min(5, Number(count) || 4));
      return "★".repeat(n) + "☆".repeat(5 - n);
    }}

    function getTabDefinitions() {{
      const unreadCount = ARTICLES.filter(a => !readSet.has(a.id)).length;
      const readCount = ARTICLES.filter(a => readSet.has(a.id)).length;
      const top10Count = ARTICLES.filter(a => a.is_top10).length;

      const tabs = [
        {{ id: "unread", label: "未読", icon: "📥", count: unreadCount }},
        {{ id: "top10", label: "注目10個ピックアップ", icon: "🔥", count: top10Count }},
      ];

      CATEGORIES.forEach(cat => {{
        const cCount = ARTICLES.filter(a => a.category === cat.id).length;
        tabs.push({{ id: cat.id, label: cat.label, icon: cat.icon, count: cCount }});
      }});

      tabs.push({{ id: "read", label: "既読", icon: "✅", count: readCount }});
      return tabs;
    }}

    function renderTabs() {{
      const tabsBar = document.getElementById("tabsBar");
      const tabs = getTabDefinitions();
      tabsBar.innerHTML = tabs.map(t => `
        <button class="tab-btn ${{activeTab === t.id ? 'active' : ''}}" data-tab-id="${{t.id}}">
          <span>${{t.icon}} ${{t.label}}</span>
          <span class="tab-badge">${{t.count}}</span>
        </button>
      `).join("");

      tabsBar.querySelectorAll(".tab-btn").forEach(btn => {{
        btn.addEventListener("click", () => {{
          activeTab = btn.getAttribute("data-tab-id");
          syncBottomNav();
          renderAll();
        }});
      }});

      const unreadCount = ARTICLES.filter(a => !readSet.has(a.id)).length;
      const readCount = ARTICLES.filter(a => readSet.has(a.id)).length;
      document.getElementById("bottomUnreadCount").textContent = unreadCount;
      document.getElementById("bottomReadCount").textContent = readCount;
    }}

    function syncBottomNav() {{
      document.querySelectorAll(".bottom-nav .nav-item").forEach(btn => {{
        const t = btn.getAttribute("data-tab");
        btn.classList.toggle("active", t === activeTab);
      }});
    }}

    function filterArticles() {{
      let list = ARTICLES.slice();

      if (activeTab === "unread") {{
        list = list.filter(a => !readSet.has(a.id));
      }} else if (activeTab === "read") {{
        list = list.filter(a => readSet.has(a.id));
      }} else if (activeTab === "top10") {{
        list = list.filter(a => a.is_top10).sort((a, b) => (a.top_rank || 99) - (b.top_rank || 99));
      }} else {{
        list = list.filter(a => a.category === activeTab);
      }}

      if (searchQuery.trim() !== "") {{
        const q = searchQuery.trim().toLowerCase();
        list = list.filter(a =>
          (a.title_ja || "").toLowerCase().includes(q) ||
          (a.original_title || "").toLowerCase().includes(q) ||
          (a.source_name || "").toLowerCase().includes(q) ||
          (a.summary_ja || "").toLowerCase().includes(q)
        );
      }}

      return list;
    }}

    function escapeHtml(str) {{
      return String(str || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}

    function renderFeed() {{
      const container = document.getElementById("feedContainer");
      const list = filterArticles();

      let bannerHtml = "";
      if (activeTab === "top10") {{
        bannerHtml = `
          <div class="section-banner">
            <span>🔥 <strong>本日の注目10個ピックアップ</strong>（重要度・注目度順）</span>
          </div>
        `;
      }} else if (activeTab === "read" && list.length > 0) {{
        bannerHtml = `
          <div class="section-banner">
            <span>✅ 読んだ記事一覧（${{list.length}}件）</span>
            <button id="resetReadBtn" class="action-chip">すべて未読に戻す</button>
          </div>
        `;
      }}

      if (list.length === 0) {{
        const emptyMsg = activeTab === "unread"
          ? "本日の未読ニュースはすべてチェック済みです！🎉<br><small>「既読」タブまたは「注目10個ピックアップ」から見返せます。</small>"
          : "該当するニュース記事がありません。";
        container.innerHTML = bannerHtml + `
          <div class="empty-state">
            <div class="empty-icon">${{activeTab === "unread" ? "✨" : "📭"}}</div>
            <div>${{emptyMsg}}</div>
          </div>
        `;
        bindBannerEvents();
        return;
      }}

      const cardsHtml = list.map(art => {{
        const isRead = readSet.has(art.id);
        const catMeta = getCategoryMeta(art.category);
        const showOrig = art.lang === "en" && art.original_title && art.original_title !== art.title_ja;
        const pointsHtml = (art.summary_points || []).map(p => `<li>${{escapeHtml(p)}}</li>`).join("");

        return `
          <article class="news-card ${{art.is_top10 ? 'top-pick' : ''}} ${{isRead ? 'is-read' : ''}}" data-article-id="${{escapeHtml(art.id)}}">
            <div class="card-meta">
              <div class="meta-left">
                ${{art.is_top10 && art.top_rank <= 10 ? `<span class="rank-badge">注目 #${{art.top_rank}}</span>` : ''}}
                <span class="cat-tag">${{catMeta.icon}} ${{escapeHtml(catMeta.label)}}</span>
                <span class="source-name">${{escapeHtml(art.source_name)}}</span>
              </div>
              <span class="stars-box" title="AI注目度 星${{art.stars}}">${{formatStars(art.stars)}}</span>
            </div>

            <h2 class="card-title">
              <a href="${{escapeHtml(art.url)}}" target="_blank" rel="noopener noreferrer" data-open-id="${{escapeHtml(art.id)}}">
                ${{escapeHtml(art.title_ja)}}
              </a>
            </h2>

            ${{showOrig ? `<p class="orig-title">原文: ${{escapeHtml(art.original_title)}}</p>` : ''}}

            ${{art.pick_reason ? `<div class="pick-reason">💡 ${{escapeHtml(art.pick_reason)}}</div>` : ''}}

            <div class="summary-box">
              <div class="summary-label">✨ AI 3行要約</div>
              <ul class="summary-list">
                ${{pointsHtml}}
              </ul>
            </div>

            <div class="card-footer">
              <span class="pub-time">🕒 ${{escapeHtml(art.published_at)}}</span>
              <div class="card-actions">
                <button class="btn-read-toggle ${{!isRead ? 'unread-state' : ''}}" data-toggle-id="${{escapeHtml(art.id)}}">
                  ${{isRead ? '↩ 未読に戻す' : '✓ 既読にする'}}
                </button>
                <a href="${{escapeHtml(art.url)}}" target="_blank" rel="noopener noreferrer" class="btn-open-article" data-open-id="${{escapeHtml(art.id)}}">
                  記事を読む ↗
                </a>
              </div>
            </div>
          </article>
        `;
      }}).join("");

      container.innerHTML = bannerHtml + cardsHtml;
      bindCardEvents();
      bindBannerEvents();
    }}

    function bindBannerEvents() {{
      const resetBtn = document.getElementById("resetReadBtn");
      if (resetBtn) {{
        resetBtn.addEventListener("click", () => {{
          readSet.clear();
          saveReadState();
          renderAll();
        }});
      }}
    }}

    function bindCardEvents() {{
      document.querySelectorAll("[data-toggle-id]").forEach(btn => {{
        btn.addEventListener("click", () => {{
          const id = btn.getAttribute("data-toggle-id");
          if (readSet.has(id)) {{
            readSet.delete(id);
          }} else {{
            readSet.add(id);
          }}
          saveReadState();
          renderAll();
        }});
      }});

      document.querySelectorAll("[data-open-id]").forEach(link => {{
        link.addEventListener("click", () => {{
          const id = link.getAttribute("data-open-id");
          if (!readSet.has(id)) {{
            readSet.add(id);
            saveReadState();
            setTimeout(renderAll, 250);
          }}
        }});
      }});
    }}

    function renderAll() {{
      renderTabs();
      renderFeed();
    }}

    document.getElementById("searchInput").addEventListener("input", e => {{
      searchQuery = e.target.value || "";
      renderFeed();
    }});

    document.getElementById("markAllBtn").addEventListener("click", () => {{
      const visible = filterArticles();
      visible.forEach(a => readSet.add(a.id));
      saveReadState();
      renderAll();
    }});

    document.querySelectorAll(".bottom-nav .nav-item").forEach(btn => {{
      btn.addEventListener("click", () => {{
        activeTab = btn.getAttribute("data-tab");
        syncBottomNav();
        renderAll();
        window.scrollTo({{ top: 0, behavior: "smooth" }});
      }});
    }});

    renderAll();
  </script>
</body>
</html>
"""


def main() -> None:
    config = load_json(FEEDS_PATH, {})
    seen_data = load_json(SEEN_PATH, {"seen_urls": [], "updated_at": ""})
    seen_urls = set(seen_data.get("seen_urls", []))

    now_dt = datetime.datetime.now(JST)

    if "--from-data" in sys.argv and os.path.exists(DATA_OUT_PATH):
        saved = load_json(DATA_OUT_PATH, {})
        summarized = saved.get("articles", [])
        updated_str = saved.get("updated_at", now_dt.strftime("%Y/%m/%d %H:%M"))
        html_content = generate_html(config, summarized, updated_str)
        with open(HTML_OUT_PATH, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"[SUCCESS] {DATA_OUT_PATH} から {HTML_OUT_PATH} を再生成しました（記事数: {len(summarized)}件）。")
        return
    lookback_hours = int(config.get("lookback_hours", 96))
    max_articles = int(config.get("max_articles", 20))
    top_picks_count = int(config.get("top_picks_count", 10))
    gemini_model = config.get("gemini_model", "gemini-2.5-flash")
    cutoff_dt = now_dt - datetime.timedelta(hours=lookback_hours)

    all_fetched: List[Dict[str, Any]] = []
    feeds = config.get("feeds", [])

    if "--sample-only" not in sys.argv:
        for feed_cfg in feeds:
            try:
                items = fetch_rss_items(feed_cfg, cutoff_dt, seen_urls)
                all_fetched.extend(items)
                print(f"[RSS] {feed_cfg['name']}: {len(items)}件取得")
            except Exception as e:
                print(f"[RSS SKIP] {feed_cfg['name']}: {e}")

    if len(all_fetched) > 0:
        # 既存の翻訳・要約キャッシュがあれば引き継ぐ
        existing_cache = {}
        if os.path.exists(DATA_OUT_PATH):
            prev_data = load_json(DATA_OUT_PATH, {})
            for prev_art in prev_data.get("articles", []):
                if prev_art.get("url") and prev_art.get("title_ja"):
                    existing_cache[prev_art["url"]] = prev_art

        selected = select_balanced_articles(all_fetched, max_articles)
        for art in selected:
            cached = existing_cache.get(art["url"])
            if cached:
                art["title_ja"] = cached.get("title_ja", art.get("title_ja"))
                art["summary_points"] = cached.get("summary_points", art.get("summary_points"))
                art["pick_reason"] = cached.get("pick_reason", art.get("pick_reason"))
                art["stars"] = cached.get("stars", art.get("base_stars", 4))

        summarized = summarize_with_gemini_batch(selected, gemini_model, top_picks_count)
        # 一度掲載した記事URLを記録（最大1000件保持）
        if "--record-seen" in sys.argv or os.environ.get("GITHUB_ACTIONS") == "true":
            new_seen = list(seen_data.get("seen_urls", []))
            for art in summarized:
                if art["url"] not in seen_urls:
                    new_seen.append(art["url"])
            save_json(
                SEEN_PATH,
                {
                    "seen_urls": new_seen[-1000:],
                    "updated_at": now_dt.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                },
            )
    else:
        if os.path.exists(DATA_OUT_PATH):
            print("[INFO] オフライン環境のため既存の articles_data.json を保持します。")
            prev_data = load_json(DATA_OUT_PATH, {})
            summarized = prev_data.get("articles", get_fallback_sample_articles(now_dt))
        else:
            print("[INFO] ライブRSSが0件（またはオフライン環境）のため、全6カテゴリの初期プレビュー用AIニュースデータを生成します。")
            summarized = get_fallback_sample_articles(now_dt)

    updated_str = now_dt.strftime("%Y/%m/%d %H:%M")
    save_json(
        DATA_OUT_PATH,
        {
            "updated_at": updated_str,
            "count": len(summarized),
            "articles": summarized,
        },
    )

    html_content = generate_html(config, summarized, updated_str)
    with open(HTML_OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"[SUCCESS] {HTML_OUT_PATH} を生成しました（記事数: {len(summarized)}件）。")


if __name__ == "__main__":
    main()
