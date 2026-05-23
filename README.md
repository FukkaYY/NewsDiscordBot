# News Discord Bot

Gemini APIを使用して、最新のニュースを自動的にDiscordチャンネルに配信するBotです。

## 機能

- 毎日 9:00 と 17:00 (JST) にニュースを5件配信します。
- ニュースは DuckDuckGo News で最新記事を取得し、Gemini (gemini-1.5-flash) が厳選・要約します。
- 過去に配信したニュースとの重複を避けます。
- `/setup` コマンドで配信チャンネルを設定できます（管理者のみ）。
- `/test_news` コマンドで即座に配信テストが可能です（管理者のみ）。

## セットアップ

1. 必要なライブラリのインストール:

    ```bash
    pip install -r requirements.txt
    ```

2. `.env` ファイルの作成:

    `.env.example` をコピーして `.env` を作成し、各トークンを設定してください。
    - `DISCORD_BOT_TOKEN`: Discord Developer Portalから取得
    - `GEMINI_API_KEY`: Google AI Studioから取得

3. Botの実行:

    ```bash
    python main.py
    ```

## 使用方法

1. Botをサーバーに招待します。
2. ニュースを配信したいチャンネルで `/setup` コマンドを実行します（管理者権限が必要）。
3. 設定した時間に自動的にニュースが届きます。
4. **関心度の評価**:
   - 届いたニュースの下部にある 1〜5 のボタンを押して評価してください。
   - 評価が高いジャンルのニュースが、次回の配信で選ばれやすくなります。
   - 評価するとボタンが緑色に変わり、そのニュースへの評価が確定します。
