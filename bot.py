"""Bale mention forwarder. Python 3.10+, standard library only."""

import argparse
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("bale-ticket")


class APIError(Exception):
    def __init__(self, code, description, retry_after=15):
        super().__init__(description)
        self.code = code
        self.retry_after = retry_after


class BaleAPI:
    def __init__(self, token):
        self.token = token

    def call(self, method, **params):
        request = Request(
            f"https://tapi.bale.ai/bot{self.token}/{method}",
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=45) as response:
                payload = json.load(response)
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read())
            except (ValueError, UnicodeError):
                raise APIError(exc.code, f"HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError, ValueError):
            # Network exceptions can contain the secret token in the URL.
            raise APIError(0, "Network error or invalid API response") from None
        if not payload.get("ok"):
            description = str(payload.get("description", "API error"))
            raise APIError(
                payload.get("error_code", 0),
                description.replace(self.token, "[REDACTED]"),
                payload.get("parameters", {}).get("retry_after", 15),
            )
        return payload["result"]


def load_config(path, list_chats=False):
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("config.json must contain an object.")
    config["bot_token"] = os.environ.get("BALE_BOT_TOKEN") or config.get("bot_token", "")
    if not isinstance(config["bot_token"], str) or not config["bot_token"].strip():
        raise ValueError("Set bot_token in config.json or BALE_BOT_TOKEN.")
    if list_chats:
        return config
    if "BALE_TARGET_USERNAMES" in os.environ:
        config["target_usernames"] = [name.strip() for name in os.environ["BALE_TARGET_USERNAMES"].split(",") if name.strip()]
    if "BALE_DESTINATION_CHAT_ID" in os.environ:
        try:
            config["destination_chat_ids"] = [int(item.strip()) for item in os.environ["BALE_DESTINATION_CHAT_ID"].split(",")]
        except ValueError:
            raise ValueError("BALE_DESTINATION_CHAT_ID must contain comma-separated numeric group IDs.") from None
    if "BALE_SOURCE_CHAT_IDS" in os.environ:
        try:
            source_ids = os.environ["BALE_SOURCE_CHAT_IDS"].strip()
            config["source_chat_ids"] = [int(item.strip()) for item in source_ids.split(",")] if source_ids else []
        except ValueError:
            raise ValueError("BALE_SOURCE_CHAT_IDS must be comma-separated numeric group IDs.") from None
    names = config.get("target_usernames", [])
    if not isinstance(names, list) or not names:
        raise ValueError("target_usernames must be a nonempty list of @usernames.")
    names = [name.strip().lstrip("@").lower() for name in names]
    if any(not re.fullmatch(r"[a-z0-9_]+", name) for name in names):
        raise ValueError("Use real usernames without spaces in target_usernames.")
    if any(name in {"username1", "username2"} for name in names):
        raise ValueError("Replace the sample usernames in config.json.")
    config["target_usernames"] = set(names)
    destinations = config.get("destination_chat_ids", [config.get("destination_chat_id")])
    if not isinstance(destinations, list) or not destinations or any(type(item) is not int or item == 0 for item in destinations):
        raise ValueError("destination_chat_id(s) must contain numeric destination group IDs.")
    config["destination_chat_ids"] = list(dict.fromkeys(destinations))
    config["destination_chat_id"] = config["destination_chat_ids"][0]
    sources = config.get("source_chat_ids", [])
    if not isinstance(sources, list) or any(type(item) is not int for item in sources):
        raise ValueError("source_chat_ids must be a list of numeric group IDs, or [].")
    return config


def mentioned(message, targets):
    for text_key, entity_key in (("text", "entities"), ("caption", "caption_entities")):
        text = message.get(text_key) or ""
        entities = message.get(entity_key) or []
        for entity in entities:
            if entity.get("type") != "mention":
                continue
            start = entity["offset"] * 2
            end = start + entity["length"] * 2
            name = text.encode("utf-16-le")[start:end].decode("utf-16-le", errors="replace")
            if name.lstrip("@").lower() in targets:
                return True
        # Some Bale clients omit entities; match complete @handles, not emails.
        if not entities:
            handles = re.findall(r"(?<![\w@/])@([A-Za-z0-9_]+)(?![\w@])", text)
            if any(name.lower() in targets for name in handles):
                return True
    return False


def should_forward(message, config):
    chat = message.get("chat", {})
    return (
        chat.get("type") in {"group", "supergroup"}
        and chat.get("id") not in config.get("destination_chat_ids", [config.get("destination_chat_id")])
        and (not config.get("source_chat_ids") or chat.get("id") in config["source_chat_ids"])
        and mentioned(message, config["target_usernames"])
    )


def reply_button(forwarded_id, source, username):
    group = username if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]+", username) else str(source)
    return {"inline_keyboard": [
        [{"text": "رفتن به گروه مبدأ ↗", "url": f"https://ble.ir/{group}"}],
        [{"text": "جواب به این پیام", "callback_data": f"reply:{forwarded_id}"}],
    ]}


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value INTEGER);
            CREATE TABLE IF NOT EXISTS jobs (
                source INTEGER, message INTEGER, destination INTEGER,
                done INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (source, message, destination)
            );
            CREATE TABLE IF NOT EXISTS replies (
                destination INTEGER, message INTEGER, source INTEGER,
                original_message INTEGER, text TEXT,
                done INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (destination, message)
            );
            CREATE TABLE IF NOT EXISTS reply_selections (
                destination INTEGER, user_id INTEGER, source INTEGER, original_message INTEGER,
                expires REAL, PRIMARY KEY (destination, user_id)
            );
            CREATE TABLE IF NOT EXISTS callback_answers (
                id TEXT PRIMARY KEY, text TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        for name, kind in (("source_url", "TEXT"), ("forwarded_id", "INTEGER"), ("source_username", "TEXT")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
        self.db.commit()

    def offset(self):
        row = self.db.execute("SELECT value FROM state WHERE key='offset'").fetchone()
        return row[0] if row else 0

    def ingest(self, updates, config):
        destinations = config.get("destination_chat_ids", [config.get("destination_chat_id")])
        with self.db:
            for update in sorted(updates, key=lambda item: item["update_id"]):
                if update["update_id"] < self.offset():
                    continue
                message = update.get("message") or update.get("edited_message")
                callback = update.get("callback_query")
                if callback:
                    callback_chat = (callback.get("message") or {}).get("chat", {}).get("id")
                    user_id = (callback.get("from") or {}).get("id")
                    match = re.fullmatch(r"reply:(\d+)", callback.get("data") or "")
                    original = None
                    if callback_chat in destinations and user_id is not None and match:
                        original = self.db.execute(
                            "SELECT source,message FROM jobs WHERE destination=? AND forwarded_id=?",
                            (callback_chat, int(match.group(1))),
                        ).fetchone()
                    if original:
                        self.db.execute("INSERT OR REPLACE INTO reply_selections VALUES (?,?,?,?,?)",
                                        (callback_chat, user_id, *original, time.time() + 600))
                        answer = "پاسخ بعدی خود را در گروه بنویسید. برای لغو /cancel را بفرستید."
                    else:
                        answer = "پیام اصلی پیدا نشد."
                    self.db.execute("INSERT OR IGNORE INTO callback_answers VALUES (?,?)", (callback["id"], answer))
                destination = (message or {}).get("chat", {}).get("id")
                if update.get("message") and destination in destinations:
                    replied_id = (message.get("reply_to_message") or {}).get("message_id")
                    text = message.get("text")
                    user_id = (message.get("from") or {}).get("id")
                    if user_id is not None:
                        self.db.execute("DELETE FROM reply_selections WHERE destination=? AND user_id=? AND expires<?",
                                        (destination, user_id, time.time()))
                        if text and text.strip() == "/cancel":
                            self.db.execute("DELETE FROM reply_selections WHERE destination=? AND user_id=?",
                                            (destination, user_id))
                        elif text and not text.startswith("/") and replied_id is None:
                            original = self.db.execute(
                                "SELECT source,original_message FROM reply_selections WHERE destination=? AND user_id=?",
                                (destination, user_id),
                            ).fetchone()
                            if original:
                                self.db.execute(
                                    "INSERT OR IGNORE INTO replies (destination,message,source,original_message,text) VALUES (?,?,?,?,?)",
                                    (destination, message["message_id"], *original, text),
                                )
                                self.db.execute("DELETE FROM reply_selections WHERE destination=? AND user_id=?",
                                                (destination, user_id))
                    if replied_id is not None and text:
                        original = self.db.execute(
                            "SELECT source,message FROM jobs WHERE destination=? AND forwarded_id=?",
                            (destination, replied_id),
                        ).fetchone()
                        if original:
                            self.db.execute(
                                "INSERT OR IGNORE INTO replies (destination,message,source,original_message,text) VALUES (?,?,?,?,?)",
                                (destination, message["message_id"], *original, text),
                            )
                            if user_id is not None:
                                self.db.execute("DELETE FROM reply_selections WHERE destination=? AND user_id=?",
                                                (destination, user_id))
                if message and should_forward(message, config):
                    for destination in destinations:
                        self.db.execute(
                            "INSERT OR IGNORE INTO jobs (source,message,destination,source_username) VALUES (?,?,?,?)",
                            (message["chat"]["id"], message["message_id"], destination,
                             message["chat"].get("username")),
                        )
                self.db.execute("INSERT OR REPLACE INTO state VALUES ('offset', ?)",
                                (update["update_id"] + 1,))

    def forward_pending(self, api):
        rows = self.db.execute(
            "SELECT source,message,destination,source_username,forwarded_id FROM jobs WHERE done=0 AND next_try<=? LIMIT 20",
            (time.time(),),
        ).fetchall()
        for source, message, destination, username, forwarded_id in rows:
            try:
                if forwarded_id is None:
                    forwarded = api.call("forwardMessage", chat_id=destination,
                                         from_chat_id=source, message_id=message)
                    forwarded_id = forwarded["message_id"]
                    # Save before sending the button so a failed reply never re-forwards.
                    with self.db:
                        self.db.execute(
                            "UPDATE jobs SET forwarded_id=? WHERE source=? AND message=? AND destination=?",
                            (forwarded_id, source, message, destination))
                api.call("sendMessage", chat_id=destination,
                         reply_to_message_id=forwarded_id,
                         text="برای رفتن به گروه مبدأ یا پاسخ از این گروه، یکی از دکمه‌ها را بزنید. برای لغو پاسخ /cancel را بفرستید.",
                         reply_markup=reply_button(forwarded_id, source, username))
            except APIError as exc:
                if exc.code in {401, 409}:
                    raise
                LOG.warning("%s failed for %s/%s: API %s: %s (will retry)",
                            "Source button" if forwarded_id is not None else "Forward",
                            source, message, exc.code, exc)
                delay = max(5, float(exc.retry_after), 300 if exc.code in {400, 403} else 15)
                with self.db:
                    self.db.execute("UPDATE jobs SET next_try=? WHERE source=? AND message=? AND destination=?",
                                    (time.time() + delay, source, message, destination))
                if exc.code == 429:
                    break
            else:
                with self.db:
                    self.db.execute("UPDATE jobs SET done=1 WHERE source=? AND message=? AND destination=?",
                                    (source, message, destination))
                LOG.info("Forwarded %s/%s", source, message)

    def send_pending_replies(self, api):
        rows = self.db.execute(
            "SELECT destination,message,source,original_message,text FROM replies WHERE done=0 AND next_try<=? LIMIT 20",
            (time.time(),),
        ).fetchall()
        for destination, message, source, original_message, text in rows:
            try:
                api.call("sendMessage", chat_id=source, reply_to_message_id=original_message, text=text)
            except APIError as exc:
                if exc.code in {401, 409}:
                    raise
                LOG.warning("Reply failed for %s/%s: API %s: %s (will retry)", destination, message, exc.code, exc)
                delay = max(5, float(exc.retry_after), 300 if exc.code in {400, 403} else 15)
                with self.db:
                    self.db.execute("UPDATE replies SET next_try=? WHERE destination=? AND message=?",
                                    (time.time() + delay, destination, message))
                if exc.code == 429:
                    break
            else:
                with self.db:
                    self.db.execute("UPDATE replies SET done=1 WHERE destination=? AND message=?",
                                    (destination, message))
                LOG.info("Replied to %s/%s", source, original_message)

    def answer_callbacks(self, api):
        for callback_id, text in self.db.execute("SELECT id,text FROM callback_answers LIMIT 20").fetchall():
            try:
                api.call("answerCallbackQuery", callback_query_id=callback_id, text=text, show_alert=True)
            except APIError as exc:
                if exc.code in {401, 409}:
                    raise
                LOG.warning("Callback answer failed: API %s: %s", exc.code, exc)
                if exc.code not in {400, 403}:
                    continue
            with self.db:
                self.db.execute("DELETE FROM callback_answers WHERE id=?", (callback_id,))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-chats", action="store_true", help="Show groups in pending updates; no forwarding")
    args = parser.parse_args()
    config = load_config(ROOT / "config.json", args.list_chats)
    api = BaleAPI(config["bot_token"])
    me = api.call("getMe")
    if api.call("getWebhookInfo").get("url"):
        raise ValueError("An active webhook exists. Disable it before using polling.")
    if args.list_chats:
        groups = {}
        for update in api.call("getUpdates", timeout=0, limit=100):
            msg = update.get("message") or update.get("edited_message") or {}
            chat = msg.get("chat", {})
            if chat.get("type") in {"group", "supergroup"}:
                groups[chat["id"]] = chat.get("title", "")
        for chat_id, title in groups.items():
            print(f"{chat_id}\t{title}")
        if not groups:
            print("No pending group messages. Add the bot to a group and send a fresh message.")
        return
    store = Store(ROOT / f"state-{me['id']}.sqlite3")
    LOG.info("Bale bot @%s started. Press Ctrl+C to stop.", me.get("username", me["id"]))
    while True:
        try:
            store.forward_pending(api)
            store.send_pending_replies(api)
            store.answer_callbacks(api)
            updates = api.call("getUpdates", offset=store.offset(), timeout=10, limit=100)
            store.ingest(updates, config)
            if not updates:
                time.sleep(1)
        except APIError as exc:
            if exc.code in {401, 409}:
                raise
            LOG.warning("Bale API error: %s; retrying", exc)
            time.sleep(max(5, float(exc.retry_after)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        LOG.info("Stopped.")
    except (ValueError, APIError, OSError) as error:
        LOG.error("%s", error)
        raise SystemExit(1)
