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
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    config["bot_token"] = os.environ.get("BALE_BOT_TOKEN") or config.get("bot_token", "")
    if not config["bot_token"].strip():
        raise ValueError("Set bot_token in config.json or BALE_BOT_TOKEN.")
    if list_chats:
        return config
    names = config.get("target_usernames", [])
    if not isinstance(names, list) or not names:
        raise ValueError("target_usernames must be a nonempty list of @usernames.")
    names = [name.strip().lstrip("@").lower() for name in names]
    if any(not re.fullmatch(r"[a-z0-9_]+", name) for name in names):
        raise ValueError("Use real usernames without spaces in target_usernames.")
    if any(name in {"username1", "username2"} for name in names):
        raise ValueError("Replace the sample usernames in config.json.")
    config["target_usernames"] = set(names)
    destination = config.get("destination_chat_id")
    if isinstance(destination, bool) or not isinstance(destination, int) or destination == 0:
        raise ValueError("destination_chat_id must be the numeric destination group ID.")
    sources = config.get("source_chat_ids", [])
    if not isinstance(sources, list) or any(type(item) is not int for item in sources):
        raise ValueError("source_chat_ids must be a list of numeric group IDs, or [].")
    templates = config.get("source_link_templates", {})
    if not isinstance(templates, dict):
        raise ValueError("source_link_templates must be an object keyed by source group ID.")
    for template in templates.values():
        if not isinstance(template, str) or not template.startswith("https://"):
            raise ValueError("Each source link template must be an HTTPS URL.")
        try:
            template.format(chat_id=1, message_id=1, date=1700000000, date_ms=1700000000000)
        except (KeyError, IndexError, ValueError, AttributeError):
            raise ValueError("Invalid source link template placeholders.") from None
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
        and chat.get("id") != config["destination_chat_id"]
        and (not config.get("source_chat_ids") or chat.get("id") in config["source_chat_ids"])
        and mentioned(message, config["target_usernames"])
    )


def source_message_url(message, config):
    """Use the permalink shape emitted by the official Bale web client."""
    chat = message["chat"]
    templates = config.get("source_link_templates", {})
    template = templates.get(str(chat["id"]))
    date = message.get("date")
    if not template and date is None:
        return None
    values = {"chat_id": chat["id"], "message_id": message["message_id"],
              "date": date, "date_ms": int(date * 1000) if date is not None else None}
    if template:
        return template.format(**values)
    group = chat.get("username") or f"post/{chat['id']}"
    return f"https://ble.ir/{group}/{values['message_id']}/{values['date_ms']}"


def source_button(url):
    return {"inline_keyboard": [[{"text": "رفتن به پیام اصلی ↗", "url": url}]]}


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
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        for name, kind in (("source_url", "TEXT"), ("forwarded_id", "INTEGER")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
        self.db.commit()

    def offset(self):
        row = self.db.execute("SELECT value FROM state WHERE key='offset'").fetchone()
        return row[0] if row else 0

    def ingest(self, updates, config):
        with self.db:
            for update in sorted(updates, key=lambda item: item["update_id"]):
                if update["update_id"] < self.offset():
                    continue
                message = update.get("message") or update.get("edited_message")
                if message and should_forward(message, config):
                    self.db.execute(
                        "INSERT OR IGNORE INTO jobs (source,message,destination,source_url) VALUES (?,?,?,?)",
                        (message["chat"]["id"], message["message_id"], config["destination_chat_id"],
                         source_message_url(message, config)),
                    )
                self.db.execute("INSERT OR REPLACE INTO state VALUES ('offset', ?)",
                                (update["update_id"] + 1,))

    def forward_pending(self, api):
        rows = self.db.execute(
            "SELECT source,message,destination,source_url,forwarded_id FROM jobs WHERE done=0 AND next_try<=? LIMIT 20",
            (time.time(),),
        ).fetchall()
        for source, message, destination, url, forwarded_id in rows:
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
                if url:
                    api.call("sendMessage", chat_id=destination,
                             reply_to_message_id=forwarded_id,
                             text="\u0628\u0631\u0627\u06cc \u0645\u0634\u0627\u0647\u062f\u0647\u0654 \u067e\u06cc\u0627\u0645 \u0627\u0635\u0644\u06cc\u060c \u062f\u06a9\u0645\u0647 \u06cc\u0627 \u0644\u06cc\u0646\u06a9 \u0632\u06cc\u0631 \u0631\u0627 \u0628\u0632\u0646\u06cc\u062f:\n" + url,
                             reply_markup=source_button(url))
                else:
                    LOG.warning("Legacy queued message %s/%s has no source link metadata", source, message)
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
