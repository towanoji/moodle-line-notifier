# 📚 工学院大学 Moodle 課題締切 LINE通知ツール

Moodle に登録されている課題の締切日を毎朝自動チェックし、  
**締切の前日に LINE でプッシュ通知**を送ってくれるツールです。  
GitHub Actions（無料）で動くため、**サーバー費用 0 円**で 24 時間運用できます。

---

## 通知のイメージ

```
📢 課題の締切通知 [2026/05/08]

━━━━━━━━━━
📘 情報工学特論A
📝 第3回レポート提出
⏰ 05/09(土) 23:59 🔴 明日締切
```

---

## セットアップ手順

### STEP 1 : LINE Bot（Messaging API）の準備

> ⚠️ LINE Notify は 2025年4月にサービス終了しました。  
> 現在は **LINE Messaging API** を使います（無料枠で十分動きます）。

#### 1-1. LINE Developers アカウント作成

1. [LINE Developers](https://developers.line.biz/ja/) にアクセス
2. 右上「コンソールにログイン」→ LINEアカウントでログイン
3. 「プロバイダー作成」→ 任意の名前を入力（例: `KogakuinNotifier`）

#### 1-2. Messaging API チャンネルを作成

1. 作成したプロバイダー → 「チャンネル作成」
2. **Messaging API** を選択
3. 必須項目を入力（チャンネル名: 例 `課題通知Bot`）
4. 作成完了 → 「Messaging API 設定」タブを開く

#### 1-3. チャンネルアクセストークンを取得

1. 「Messaging API 設定」→ 一番下の「チャンネルアクセストークン」
2. 「発行」ボタンをクリック
3. 表示されたトークンをコピーして**手元に保存**（`LINE_CHANNEL_ACCESS_TOKEN`）

#### 1-4. Bot を友だち追加する

1. 「Messaging API 設定」→ **QRコード**が表示されている
2. LINEアプリでそのQRコードを読み取り → Bot を友だち追加

#### 1-5. 自分の LINE User ID を取得

Bot に友だち追加すると、**Webhook で UserID が取得できます**。  
以下の簡単な方法で取得してください。

1. 「Messaging API 設定」→ **Webhook URL** に以下を設定:  
   `https://webhook.site/your-unique-id`  
   （[Webhook.site](https://webhook.site/) で無料URLを取得できます）
2. 「Webhookの利用」をオン
3. LINE で Bot に何かメッセージを送る
4. Webhook.site に届いたJSONの `source.userId` の値が **あなたのUserID**  
   （`Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` の形式）
5. コピーして保存（`LINE_USER_ID`）
6. Webhook URL は空欄に戻しても OK

> **別の方法**: Bot に「こんにちは」と送り、LINE Developers Console の  
> 「Messaging API 設定」→「Your user ID」から確認することもできます。

---

### STEP 2 : GitHub にリポジトリを作成・アップロード

#### 2-1. GitHubアカウントの用意

[GitHub](https://github.com) のアカウントを持っていない場合は作成してください。

#### 2-2. リポジトリを作成

1. GitHub → 右上「+」→「New repository」
2. Repository name: `moodle-line-notifier`（任意）
3. **Private** を選択（パスワードを守るため）
4. 「Create repository」をクリック

#### 2-3. ファイルをアップロード

以下のファイルを全部リポジトリにアップロードします。

```
moodle-line-notifier/
├── main.py
├── requirements.txt
├── .gitignore
└── .github/
    └── workflows/
        └── notify.yml
```

> **Gitに慣れていない場合:**  
> GitHub のリポジトリページ → 「uploading an existing file」からドラッグ＆ドロップで OK です。  
> `.github/workflows/notify.yml` はフォルダ構造ごと必要なので、  
> GitHub Desktop や `git` コマンドの使用を推奨します。

---

### STEP 3 : GitHub Secrets に認証情報を登録

パスワードなどの機密情報は **GitHub Secrets** に安全に保存します。  
コードには直接書かないでください。

1. リポジトリページ → 「Settings」タブ
2. 左メニュー「Secrets and variables」→「Actions」
3. 「New repository secret」で以下を1つずつ登録

| Secret 名 | 値の例 | 説明 |
|-----------|--------|------|
| `MOODLE_URL` | `https://lms.kogakuin.ac.jp` | Moodle のURL（末尾スラッシュなし）|
| `MOODLE_USERNAME` | `a123456` | 学籍番号 |
| `MOODLE_PASSWORD` | `yourpassword` | Moodle のパスワード |
| `LINE_CHANNEL_ACCESS_TOKEN` | `eyJhbG...` | STEP 1-3 で取得したトークン |
| `LINE_USER_ID` | `U1234abc...` | STEP 1-5 で取得したUserID |
| `NOTIFY_DAYS_BEFORE` | `1` | 何日前に通知するか（複数: `1,3,7`）|

> ⚠️ `MOODLE_URL` が不明な場合は、LMS にブラウザでアクセスしたときの  
> アドレスバーに表示されるURL（例: `https://lms.kogakuin.ac.jp`）を確認してください。

---

### STEP 4 : 動作確認（手動実行）

1. リポジトリ → 「Actions」タブ
2. 左側「課題締切 LINE通知」をクリック
3. 右側「Run workflow」→「Run workflow」ボタンをクリック
4. しばらく待つと実行結果が表示される
5. 成功すれば LINE に通知が届きます 🎉

---

## 自動実行スケジュール

`notify.yml` で設定されているスケジュール:

```
毎朝 7:00 JST に自動実行
```

変更したい場合は `.github/workflows/notify.yml` の `cron` 行を編集してください。  
cron の書き方: [crontab.guru](https://crontab.guru/) で確認できます。

---

## ローカルでテスト実行する場合

```bash
# リポジトリをクローン
git clone https://github.com/あなたのユーザー名/moodle-line-notifier.git
cd moodle-line-notifier

# 依存ライブラリのインストール
pip install -r requirements.txt

# .env ファイルを作成して認証情報を記入
cp .env.example .env
# → .env をエディタで開いて各値を入力

# 環境変数を読み込んで実行
export $(cat .env | grep -v '#' | xargs)
python main.py
```

---

## トラブルシューティング

### `Moodle ログイン失敗` と表示される

- `MOODLE_USERNAME` と `MOODLE_PASSWORD` が正しいか確認
- `MOODLE_URL` が正しいか確認（末尾に `/` がないか）
- Moodle の「モバイルサービス」が有効になっているか確認  
  （通常は大学のLMSでは有効になっています）

### LINE に届かない

- `LINE_CHANNEL_ACCESS_TOKEN` が正しいか確認（有効期限切れに注意）
- `LINE_USER_ID` が正しいか確認（`U` から始まる32文字）
- Bot をLINEで友だち追加しているか確認

### 課題が0件と表示される

- 現在の学期に履修登録がされているか確認
- 課題に締切日が設定されているか確認（締切なし課題は通知されません）

---

## カスタマイズ

### 通知タイミングを変えたい

`NOTIFY_DAYS_BEFORE` Secret を変更します:
- `1` → 前日のみ
- `1,3` → 前日と3日前
- `1,3,7` → 前日・3日前・1週間前

### 通知時刻を変えたい

`.github/workflows/notify.yml` の `cron` を編集:

```yaml
# 例: 毎朝 8:00 JST = UTC 23:00
- cron: '0 23 * * *'
```

---

## ライセンス

MIT License - 自由に改変・再配布できます。
