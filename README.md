# 最新AI系ニュースまとめ（自分専用・毎朝自動更新アプリ）

YouTube動画で紹介されている構成と同じ、**完全無料・PC電源オフで毎朝自動更新されるAIニュースまとめWebアプリ**です。

## ファイル構成
- `feeds.json` : ニュース取得元（12サイト・6カテゴリ：企業発表 / 海外 / 国内 / 開発者 / 論文 / PIVOT）の設定ファイル。URLを追加・編集するだけで情報源を自由に変更できます。
- `generate_news.py` : 外部ライブラリ不要（Python標準機能のみ）で動くメインスクリプト。RSS取得 → 重複除外 → Gemini (Flash) APIで1日1回まとめて日本語見出し・3行要約・星評価・注目TOP10生成 → `index.html` 出力を行います。
- `generate_icons.py` : iPhone / Android ホーム画面追加用のアプリアイコン（`apple-touch-icon.png`, `icon-192.png`, `icon-512.png`）と `manifest.json` を自動生成するスクリプト。
- `seen_articles.json` : 一度掲載した記事URLを記録し、翌日から自動で除外するための履歴ファイル。
- `.github/workflows/daily_news.yml` : 毎朝 5:30（JST）にGitHubのクラウド上で自動実行するための指示書（GitHub Actions）。
- `index.html` : 生成されたスマホ最適化Webアプリ本体。

## ローカルでの確認方法
`index.html` をブラウザで開くか、ローカルサーバーで確認できます。
```bash
python3 -m http.server 8765 --directory /Users/masashi/Antigravity/ai-news-app
```
ブラウザで `http://localhost:8765` にアクセスすると、スマホアプリ画面を確認できます。
