# Telegram清算流速分析Bot

このBotは、Telegramチャンネルの清算メッセージをリアルタイムで監視し、Discordに要約された通知を送信します。  

## 主な機能

本Botは、ユーザー様からご提案いただいた以下の6つの機能を統合し、レイヤー化されたアーキテクチャで実装されています。

*   **L0: イベント収集**: Telegramメッセージから清算イベントの `時刻`、`銘柄`、`方向`、そして `清算額` を正確に抽出・数値化し、SQLiteデータベースに永続化します。
*   **L1: シンプル流速スコア（基本トリガー）**:
    *   直近N秒間（デフォルト5分間）の清算総額を基に、USD/秒単位での流速を計算します。
    *   この流速が設定した閾値 (`BASE_THRESHOLD_USD_PER_SEC`) を超えた場合にのみ、Botは「アクティブ」状態に遷移し、後続の高度な分析を開始します。
*   **L2: 状況解析（高度な分析指標）**: Botが「アクティブ」状態の間、以下の指標を計算し、通知内容を豊かにします。
    *   **② 件数×金額の「密度」**: 清算イベントの件数、総額、平均イベントサイズを計算し、小口が連続する相場や大口の単発清算を判別します。
    *   **③ 加速度（流速の変化）**: 直近の流速と、その前の期間の流速を比較し、流速の急激な変化（加速）を検知します。これにより、相場が「壊れ始めた瞬間」を捉えやすくなります。
    *   **④ シンボル集中型アラート**: 清算総額における特定の銘柄（例: BTC, ETH, SOL）の占有率を計算し、どの銘柄が市場を主導しているかを示します。
    *   **⑤ ロング・ショート偏り検知**: ロング清算とショート清算の総額比率を計算し、相場の方向性（例: ロングの焼き払い相場）を判断する材料を提供します。
*   **L3: 段階的まとめ通知（スパム防止）**:
    *   Botは`IDLE`（待機）と`ACTIVE`（分析・バッファリング）の状態を遷移します。
    *   `ACTIVE`状態の間は個別の通知をせず、すべての清算イベントをバッファリングします。
    *   流速が閾値を下回ってから一定時間経過すると、`ACTIVE`状態中に収集した全データを基に、上記の分析結果を盛り込んだ**包括的なサマリー通知**をDiscordに1回だけ送信します。これにより、通知のスパムを防ぎ、重要な情報に焦点を当てます。

## 前提条件

*   お使いのシステムにDockerとDocker Composeがインストールされていること。
*   TelegramアカウントとDiscordのWebhook URLを持っていること。

## セットアップ手順

### 1. API IDとAPI Hashの取得

TelegramのAPI情報を取得します。

1.  [https://my.telegram.org](https://my.telegram.org) にアクセスしてログインします。
2.  "API development tools" をクリックし、`api_id`と`api_hash`を控えます。

### 2. Discord Webhook URLの取得

通知を送信したいDiscordチャンネルのWebhook URLを取得します。
`チャンネル設定 > 連携サービス > ウェブフック > 新しいウェブフックを作成` から取得できます。

### 3. 環境変数の設定

`.env.example`ファイルをコピーして、`.env`ファイルを作成します。

```bash
cp .env.example .env
```

作成した`.env`ファイルを開き、必要な情報を記入してください。
特に、以下の**新しい設定項目**に注意し、ご自身の`.env`ファイルにも追記・設定してください。

```dotenv
# Telegram API Credentials
API_ID=YOUR_API_ID
API_HASH=YOUR_API_HASH_STRING

# Session name for telethon
SESSION_NAME=my_session

# Target channel username
CHANNEL_USERNAME=hyperliquidliqs

# Discord Webhook URL
DISCORD_WEBHOOK_URL=YOUR_DISCORD_WEBHOOK_URL

# --- Bot Configuration ---
# Number of past messages to load on startup
MESSAGE_HISTORY_LIMIT=50 # Bot起動時に初期状態で読み込む過去メッセージの件数

# --- New Liquidation Analysis Configuration ---
# L1: 基本流速スコアの閾値 (USD/sec)。この値を超えるとBotがACTIVE状態に遷移。
BASE_THRESHOLD_USD_PER_SEC=20000 

# L1/L2: 流速や各種分析の対象となる時間窓 (秒)。推奨は300秒(5分)。
ANALYSIS_WINDOW_SECONDS=300

# L1: 流速チェックを行う間隔 (秒)。短すぎるとDBへの負荷が増加。
MONITORING_INTERVAL_SECONDS=10 

# L2: 加速度の閾値。現在の流速が前回の流速の何倍になったら「CRITICAL」とするか。
ACCELERATION_THRESHOLD=3.0

# L2: シンボル集中度の閾値。特定の銘柄の清算額が総清算額のこの比率を超えるとDominanceとして認識。
DOMINANCE_THRESHOLD=0.75

# L2: ロング・ショート偏りの閾値。Long清算額 / (Long+Short清算額) がこの比率を超えるとLong Flushとして認識。
BIAS_THRESHOLD=0.85

# L3: サマリー通知後、次にサマリー通知を送信するまでのクールダウン時間 (秒)。
SUMMARY_COOLDOWN_SECONDS=60

# L3: 流速がBASE_THRESHOLDを下回ってからACTIVE状態を終了するまでの猶予期間 (秒)。
ACTIVE_IDLE_TRANSITION_GRACE_PERIOD_SECONDS=30
```

### 4. Dockerイメージのビルド

```bash
docker-compose build
```

### 5. 初回実行とTelegram認証

（まだ認証を済ませていない場合のみ）
初めて実行する際は、Telegramの認証が必要です。以下のコマンドを**フォアグラウンドで**実行し、画面の指示に従ってください。

```bash
docker-compose run --rm app
```
認証が完了すると、`my_session.session`ファイルと`bot_data.db`ファイルがプロジェクトフォルダに作成されます。確認できたら`Ctrl+C`で一旦停止して大丈夫です。

### 6. Botの起動（2回目以降）

認証が完了していれば、以下のコマンドでBotをバックグラウンドで起動できます。

```bash
docker-compose up -d
```

### 生成されるファイル

Botを実行すると、プロジェクトフォルダに以下のファイルが自動で作成・管理されます。
*   `my_session.session`: Telegramのログインセッション情報。
*   `bot_data.db`: 清算履歴と通知クールダウン情報を保存するSQLiteデータベースファイル。

### 8. 動作ログの確認

Botの動作状況は、以下のコマンドで確認できます。

```bash
docker-compose logs -f
```

### 9. Botの停止

Botを停止するには、以下のコマンドを実行します。

```bash
docker-compose down
```
---

