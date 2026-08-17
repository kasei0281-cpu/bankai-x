import base64
import hashlib
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, quote

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_session import Session
from werkzeug.middleware.proxy_fix import ProxyFix

ROOT = Path(__file__).resolve().parent
VIDEO_PATH = ROOT / "static" / "bankai.mp4"

X_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_MEDIA_INITIALIZE_URL = "https://api.x.com/2/media/upload/initialize"
X_MEDIA_STATUS_URL = "https://api.x.com/2/media/upload"
X_POST_URL = "https://api.x.com/2/tweets"
X_ME_URL = "https://api.x.com/2/users/me"

POST_TEXT = "卍！！解！！！\nhttps://bankai-x.onrender.com"
SCOPES = "tweet.read tweet.write users.read media.write offline.access"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_TYPE="filesystem",
    SESSION_FILE_DIR=os.environ.get("SESSION_FILE_DIR", "/tmp/bankai-sessions"),
    SESSION_PERMANENT=False,
    SESSION_USE_SIGNER=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
)
Session(app)


def x_client_id():
    return os.environ.get("X_CLIENT_ID", "").strip()


def x_client_secret():
    return os.environ.get("X_CLIENT_SECRET", "").strip()


def public_base_url():
    explicit = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render_url:
        return render_url
    return request.url_root.rstrip("/")


def callback_url():
    return public_base_url() + "/callback"


def configured():
    return bool(x_client_id() and x_client_secret())


def pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def token_request(data):
    cid = x_client_id()
    secret = x_client_secret()
    if not cid or not secret:
        raise RuntimeError("X_CLIENT_ID / X_CLIENT_SECRET が未設定です。")

    r = requests.post(
        X_TOKEN_URL,
        data=data,
        auth=(cid, secret),
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"X OAuth token error: HTTP {r.status_code} {r.text}")
    return r.json()


def save_tokens(payload):
    session["x_access_token"] = payload["access_token"]
    if payload.get("refresh_token"):
        session["x_refresh_token"] = payload["refresh_token"]
    session["x_expires_at"] = time.time() + int(payload.get("expires_in", 7200))


def refresh_access_token():
    refresh_token = session.get("x_refresh_token")
    if not refresh_token:
        raise RuntimeError("Xとの連携期限が切れています。もう一度連携してください。")

    payload = token_request({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    if not payload.get("refresh_token"):
        payload["refresh_token"] = refresh_token
    save_tokens(payload)
    return payload["access_token"]


def access_token():
    token = session.get("x_access_token")
    if not token:
        raise RuntimeError("先にXと連携してください。")

    expires_at = float(session.get("x_expires_at", 0))
    if expires_at and time.time() > expires_at - 120:
        return refresh_access_token()
    return token


def x_request(method, url, *, retry_auth=True, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {access_token()}"
    r = requests.request(method, url, headers=headers, timeout=120, **kwargs)

    if r.status_code == 401 and retry_auth and session.get("x_refresh_token"):
        headers["Authorization"] = f"Bearer {refresh_access_token()}"
        r = requests.request(method, url, headers=headers, timeout=120, **kwargs)
    return r


def ensure_ok(r, label):
    if not r.ok:
        raise RuntimeError(f"{label}: HTTP {r.status_code} {r.text}")
    if not r.content:
        return {}
    try:
        return r.json()
    except Exception:
        return {}


def upload_video():
    if not VIDEO_PATH.exists():
        raise RuntimeError("投稿用動画がサーバーにありません。")

    total_bytes = VIDEO_PATH.stat().st_size

    # 1) INITIALIZE
    r = x_request(
        "POST",
        X_MEDIA_INITIALIZE_URL,
        headers={"Content-Type": "application/json"},
        json={
            "media_type": "video/mp4",
            "media_category": "tweet_video",
            "total_bytes": total_bytes,
            "shared": False,
        },
    )
    data = ensure_ok(r, "動画アップロード開始に失敗")
    media_id = str((data.get("data") or {}).get("id") or "")
    if not media_id:
        raise RuntimeError(f"Xからmedia_idを取得できませんでした: {data}")

    # 2) APPEND
    # X v2 append accepts base64 media string + segment_index in JSON.
    chunk_size = 1024 * 1024
    with VIDEO_PATH.open("rb") as f:
        segment = 0
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break

            r = x_request(
                "POST",
                f"https://api.x.com/2/media/upload/{media_id}/append",
                headers={"Content-Type": "application/json"},
                json={
                    "media": base64.b64encode(chunk).decode("ascii"),
                    "segment_index": segment,
                },
            )
            ensure_ok(r, f"動画チャンク{segment + 1}のアップロードに失敗")
            segment += 1

    # 3) FINALIZE
    r = x_request(
        "POST",
        f"https://api.x.com/2/media/upload/{media_id}/finalize",
    )
    final_data = ensure_ok(r, "動画アップロード確定に失敗")

    processing = (final_data.get("data") or {}).get("processing_info") or {}
    state = processing.get("state")

    # 4) STATUS
    deadline = time.time() + 180
    while state in ("pending", "in_progress") and time.time() < deadline:
        wait_seconds = max(1, int(processing.get("check_after_secs", 1)))
        time.sleep(min(wait_seconds, 10))

        r = x_request(
            "GET",
            X_MEDIA_STATUS_URL,
            params={"media_id": media_id},
        )
        status_data = ensure_ok(r, "X側の動画処理確認に失敗")
        processing = (status_data.get("data") or {}).get("processing_info") or {}
        state = processing.get("state")

    if state == "failed":
        error = processing.get("error") or processing
        raise RuntimeError(f"X側の動画処理に失敗しました: {error}")
    if state in ("pending", "in_progress"):
        raise RuntimeError("X側の動画処理がタイムアウトしました。")

    return media_id


def create_x_post():
    media_id = upload_video()

    r = x_request(
        "POST",
        X_POST_URL,
        headers={"Content-Type": "application/json"},
        json={
            "text": POST_TEXT,
            "media": {"media_ids": [media_id]},
        },
    )
    data = ensure_ok(r, "投稿に失敗")
    post_id = str((data.get("data") or {}).get("id") or "")
    if not post_id:
        raise RuntimeError(f"投稿IDを取得できませんでした: {data}")
    return post_id


@app.get("/")
def index():
    csrf = session.get("csrf_token")
    if not csrf:
        csrf = secrets.token_urlsafe(32)
        session["csrf_token"] = csrf

    return render_template(
        "index.html",
        configured=configured(),
        connected=bool(session.get("x_access_token")),
        username=session.get("x_username"),
        callback_url=callback_url(),
        csrf_token=csrf,
    )


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/login")
def login():
    if not configured():
        return redirect(url_for("index"))

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)

    session["oauth_state"] = state
    session["pkce_verifier"] = verifier
    session["oauth_started_at"] = time.time()

    params = {
        "response_type": "code",
        "client_id": x_client_id(),
        "redirect_uri": callback_url(),
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }

    # IMPORTANT:
    # X requires RFC3986 percent encoding. quote_plus() would turn spaces
    # into "+"; using quote makes scope spaces "%20".
    query = urlencode(params, quote_via=quote)
    return redirect(X_AUTHORIZE_URL + "?" + query)


@app.get("/callback")
def callback():
    if request.args.get("error"):
        session["flash_error"] = "X連携がキャンセルされました。"
        return redirect(url_for("index"))

    code = request.args.get("code", "")
    state = request.args.get("state", "")
    expected_state = session.pop("oauth_state", "")
    verifier = session.pop("pkce_verifier", "")
    started = float(session.pop("oauth_started_at", 0) or 0)

    if not code or not state or state != expected_state or not verifier:
        session["flash_error"] = "X連携情報を確認できませんでした。もう一度連携してください。"
        return redirect(url_for("index"))

    if started and time.time() - started > 600:
        session["flash_error"] = "X連携の有効時間を超えました。もう一度連携してください。"
        return redirect(url_for("index"))

    try:
        payload = token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": callback_url(),
            "code_verifier": verifier,
        })
        save_tokens(payload)

        try:
            r = x_request("GET", X_ME_URL)
            me = ensure_ok(r, "アカウント情報取得に失敗")
            session["x_username"] = (me.get("data") or {}).get("username")
        except Exception:
            session["x_username"] = None

        session["flash_success"] = "Xとの連携が完了しました。"
    except Exception as exc:
        session["flash_error"] = str(exc)

    return redirect(url_for("index"))


@app.post("/api/bankai")
def bankai():
    csrf = request.headers.get("X-CSRF-Token", "")
    if not csrf or not secrets.compare_digest(csrf, session.get("csrf_token", "")):
        return jsonify({"error": "CSRF verification failed."}), 403

    if not session.get("x_access_token"):
        return jsonify({"error": "先にXと連携してください。"}), 401

    last_post = float(session.get("last_post_at", 0) or 0)
    if time.time() - last_post < 8:
        return jsonify({"error": "連打防止中です。数秒後にもう一度どうぞ。"}), 429

    try:
        post_id = create_x_post()
        session["last_post_at"] = time.time()
        return jsonify({
            "ok": True,
            "post_id": post_id,
            "url": f"https://x.com/i/web/status/{post_id}",
        })
    except Exception as exc:
        message = str(exc)
        if "401" in message:
            session.pop("x_access_token", None)
            session.pop("x_refresh_token", None)
            session.pop("x_expires_at", None)
        return jsonify({"error": message}), 500


@app.post("/disconnect")
def disconnect():
    csrf = request.form.get("csrf_token", "")
    if csrf and secrets.compare_digest(csrf, session.get("csrf_token", "")):
        session.clear()
    return redirect(url_for("index"))


@app.context_processor
def inject_flash():
    return {
        "flash_error": session.pop("flash_error", None),
        "flash_success": session.pop("flash_success", None),
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
