import json
import threading
import time

import httpx
import jwt

from android_use import store

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

_lock = threading.Lock()
_cache = {"token": None, "exp": 0.0}


def configured() -> bool:
    return store.SERVICE_ACCOUNT.is_file()


def _account() -> dict:
    return json.loads(store.SERVICE_ACCOUNT.read_text(encoding="utf-8"))


def _access_token(account: dict) -> str:
    with _lock:
        if _cache["token"] and time.time() < _cache["exp"] - 60:
            return _cache["token"]
        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": account["client_email"],
                "scope": SCOPE,
                "aud": account.get("token_uri", "https://oauth2.googleapis.com/token"),
                "iat": now,
                "exp": now + 3600,
            },
            account["private_key"],
            algorithm="RS256",
        )
        response = httpx.post(
            account.get("token_uri", "https://oauth2.googleapis.com/token"),
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        _cache["token"] = body["access_token"]
        _cache["exp"] = time.time() + int(body.get("expires_in", 3600))
        return _cache["token"]


def wake(fcm_token: str) -> tuple[bool, str]:
    if not configured():
        return False, f"FCM is not configured (put the Firebase service account JSON at {store.SERVICE_ACCOUNT})"
    if not fcm_token:
        return False, "the phone has not registered an FCM token yet (import google-services.json in the app)"
    try:
        account = _account()
        access = _access_token(account)
        response = httpx.post(
            f"https://fcm.googleapis.com/v1/projects/{account['project_id']}/messages:send",
            headers={"Authorization": f"Bearer {access}"},
            json={
                "message": {
                    "token": fcm_token,
                    "data": {"type": "wake"},
                    "android": {"priority": "HIGH", "ttl": "60s"},
                }
            },
            timeout=15,
        )
    except Exception as error:
        return False, f"FCM request failed: {error}"
    if response.status_code == 200:
        return True, "wake sent"
    return False, f"FCM error {response.status_code}: {response.text[:300]}"
