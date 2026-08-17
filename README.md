# 卍解 X AUTO POST — 完全WEB版

URLを開く → X連携 → 「卍解してXへ投稿」
で、同梱の `static/bankai.mp4` と「卍！！解！！！」を
連携したXアカウントへ投稿するWebアプリです。

## 仕組み
- Flaskバックエンド
- X OAuth 2.0 Authorization Code + PKCE
- scope:
  - tweet.read
  - tweet.write
  - users.read
  - media.write
  - offline.access
- 動画:
  INIT → APPEND → FINALIZE → STATUS
- 投稿:
  POST /2/tweets
- XのClient SecretやアクセストークンはHTMLへ埋め込みません。
- Xのトークンはサーバー側セッションに保存します。

## Renderで公開する手順

### 1. このフォルダをGitHubのリポジトリにアップロード
ZIPを展開し、ファイル一式をGitHubへ置きます。

### 2. RenderでWeb Serviceを作成
Render Dashboard:
New → Web Service → GitHubのリポジトリを選択

設定:
- Build Command:
  `pip install -r requirements.txt`
- Start Command:
  `gunicorn --workers 1 --threads 4 --timeout 240 --bind 0.0.0.0:$PORT app:app`

または `render.yaml` のBlueprintを使用できます。

最初はXのキーが未設定でもサイト自体は起動できます。

### 3. Renderの公開URLを開く
例:
`https://bankai-x-xxxx.onrender.com`

画面にX Developerへ登録するCallback URLが表示されます。
例:
`https://bankai-x-xxxx.onrender.com/callback`

### 4. X Developer PlatformでApp設定
OAuth 2.0を有効にし、App TypeはWeb App系のconfidential clientを使用します。

Callback URI / Redirect URL:
Render画面に表示された `/callback` URLを完全一致で登録。

Website URL:
RenderのトップURL。

Keys and Tokensから:
- Client ID
- Client Secret

を取得します。

### 5. RenderのEnvironmentに設定
Render → 対象Web Service → Environment:

- `X_CLIENT_ID` = XのClient ID
- `X_CLIENT_SECRET` = XのClient Secret
- `FLASK_SECRET_KEY` = 長いランダム文字列
  （Blueprint利用時は自動生成）

保存して再デプロイします。

### 6. 完成
公開URLへアクセス:
1. 「Xと連携して卍解」
2. Xで許可
3. 「卍解してXへ投稿」
4. 動画＋「卍！！解！！！」が投稿される

## カスタムドメインを使う場合
Renderでカスタムドメインを設定したら、
環境変数:
`PUBLIC_BASE_URL=https://あなたのドメイン`
を追加し、X Developer側のCallback URLも
`https://あなたのドメイン/callback`
に変更してください。

## 注意
- X APIの利用にはX Developer Platform側の利用条件・利用可能枠・課金が適用されます。
- Renderの再デプロイ等でサーバー側セッションが消えた場合、ユーザーはXを再連携します。
- 公開URLを知っている人は、自分のXアカウントを連携してこの動画を投稿できます。
- `X_CLIENT_SECRET` や実際のトークンをGitHubへコミットしないでください。
