import json
import os
import threading
import time
import urllib.parse
import urllib.request
import auth

_running = False
_last_message = ""
_last_chat_id = ""
_msg_lock = threading.Lock()
_state_lock = threading.Lock()

_bot_token = ""
_api_base = ""
_chat_id = ""
_chat_ids = set()
_poll_timeout = 20
_offset = None
_connected = False

_authenticated_user_id = None


def _set_last(msg, chat_id=""):
    global _last_message, _last_chat_id
    with _msg_lock:
        if _last_message == "":
            _last_message = msg
        else:
            _last_message = _last_message + " | " + msg
        if chat_id:
            _last_chat_id = chat_id


def getLastMessage():
    global _last_message
    with _msg_lock:
        tmp = _last_message
        _last_message = ""
        return tmp


def _parse_auth_candidate(msg):
    text = msg.strip()
    lower = text.lower()
    if lower.startswith("auth "):
        return text[5:].strip()
    if lower.startswith("/auth "):
        return text[6:].strip()
    return text


def _display_name(user, chat):
    username = str(user.get("username", "")).strip()
    if username:
        return f"@{username}"

    first = str(user.get("first_name", "")).strip()
    last = str(user.get("last_name", "")).strip()
    full = f"{first} {last}".strip()
    if full:
        return full

    title = str(chat.get("title", "")).strip()
    if title:
        return title

    return "telegram_user"


def _api_call(method, params=None, timeout=30, use_post=False):
    if not _api_base:
        raise RuntimeError("Telegram adapter not initialized")

    params = params or {}
    encoded = urllib.parse.urlencode(params).encode("utf-8")
    url = f"{_api_base}/{method}"

    if use_post:
        req = urllib.request.Request(url, data=encoded)
    else:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url)

    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="ignore"))

    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", f"{method} failed"))

    return payload.get("result")


def _initialize_offset():
    global _offset
    try:
        updates = _api_call("getUpdates", {"timeout": 0}, timeout=10) or []
    except Exception as exc:
        print(f"[TELEGRAM] Could not read initial offset: {exc}")
        return

    max_update = -1
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            max_update = max(max_update, update_id)

    if max_update >= 0:
        with _state_lock:
            _offset = max_update + 1


def _is_auth_command(msg):
    lower = msg.strip().lower()
    return lower.startswith("auth ") or lower.startswith("/auth ")


def _parse_chat_ids(chat_id):
    text = str(chat_id or "").strip()
    if not text:
        return []
    normalized = text.replace(";", ",").replace(" ", ",")
    return [part.strip() for part in normalized.split(",") if part.strip()]


def _first_chat_id(chat_ids):
    return chat_ids[0] if chat_ids else ""


def _is_allowed_message(chat_id, user_id, msg):
    global _chat_id, _chat_ids, _authenticated_user_id

    with _state_lock:
        if _chat_ids and chat_id not in _chat_ids:
            return "ignore"
        if not auth.is_auth_enabled():
            if not _chat_ids:
                _chat_ids.add(chat_id)
                _chat_id = chat_id
            return "allow"
        if _authenticated_user_id is not None:
            if chat_id not in _chat_ids:
                return "ignore"
            return "allow" if user_id == _authenticated_user_id else "ignore"
        if not _is_auth_command(msg):
            return "ignore"
        candidate = _parse_auth_candidate(msg)
        if auth.verify_token(candidate):
            _authenticated_user_id = user_id
            _chat_ids.add(chat_id)
            if not _chat_id:
                _chat_id = chat_id
            return "auth_bound"
        return "ignore"


def _poll_loop():
    global _connected, _offset
    print("[TELEGRAM] Polling started")

    while _running:
        try:
            params = {"timeout": int(_poll_timeout)}
            with _state_lock:
                if _offset is not None:
                    params["offset"] = _offset

            updates = _api_call("getUpdates", params=params, timeout=int(_poll_timeout) + 10) or []
            _connected = True

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    with _state_lock:
                        if _offset is None or (update_id + 1) > _offset:
                            _offset = update_id + 1

                message = (
                    update.get("message")
                    or update.get("edited_message")
                    or update.get("channel_post")
                    or update.get("edited_channel_post")
                )
                if not isinstance(message, dict):
                    continue

                text = message.get("text")
                if not text:
                    continue

                chat = message.get("chat") or {}
                user = message.get("from") or message.get("sender_chat") or {}
                chat_id = str(chat.get("id", "")).strip()
                user_id = str(user.get("id", "")).strip()
                if not chat_id:
                    continue

                state = _is_allowed_message(chat_id, user_id, text)
                display_name = _display_name(user, chat)
                if state == "allow":
                    _set_last(f"{display_name}: {text}", chat_id=chat_id)
                elif state == "auth_bound":
                    send_message(f"Authentication successful for {display_name}.", target_chat=chat_id)
        except Exception as exc:
            _connected = False
            print(f"[TELEGRAM] Poll error: {exc}")
            time.sleep(2)

    _connected = False
    print("[TELEGRAM] Polling stopped")


def start_telegram(chat_id="", poll_timeout=20):
    global _running, _bot_token, _api_base, _chat_id, _chat_ids, _poll_timeout, _offset, _connected

    proxy = auth.get_proxy_url()
    if proxy:
        _bot_token = "proxy"
        _api_base = f"{proxy}/telegram"
    else:
        _bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
        if not _bot_token:
            raise ValueError("TG_BOT_TOKEN is required")
        _api_base = f"https://api.telegram.org/bot{_bot_token}"

    _chat_ids = set(_parse_chat_ids(chat_id))
    _chat_id = _first_chat_id(_parse_chat_ids(chat_id))

    try:
        _poll_timeout = max(1, int(poll_timeout))
    except Exception:
        _poll_timeout = 20

    _offset = None
    _running = True
    _connected = False
    target_label = ",".join(_parse_chat_ids(chat_id)) or "auto-bind"
    print(f"[TELEGRAM] Starting adapter with chat target(s): {target_label}")
    _initialize_offset()

    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    return t


def stop_telegram():
    global _running
    _running = False


def send_message(text, target_chat=None):
    text = str(text).replace("\\n", "\n").replace("\r", "")
    if not text:
        return

    if target_chat is None:
        with _msg_lock:
            target_chat = _last_chat_id

    with _state_lock:
        if not target_chat:
            target_chat = _chat_id or _first_chat_id(list(_chat_ids))

    if not _connected or not target_chat:
        return

    max_len = 3900
    for i in range(0, len(text), max_len):
        chunk = text[i:i + max_len]
        if not chunk:
            continue
        try:
            _api_call(
                "sendMessage",
                {"chat_id": target_chat, "text": chunk},
                timeout=15,
                use_post=True,
            )
        except Exception as exc:
            print(f"[TELEGRAM] Send failed to {target_chat}: {exc}")
            return
