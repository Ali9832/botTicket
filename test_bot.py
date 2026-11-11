import unittest
from unittest.mock import Mock

from bot import APIError, Store, mentioned, should_forward, source_message_url


CONFIG = {"target_usernames": {"support", "admin"},
          "destination_chat_id": -99, "source_chat_ids": []}


def message(text="سلام @support", chat_id=-1):
    return {"message_id": 7, "date": 1700000000,
            "chat": {"id": chat_id, "type": "group"}, "text": text}


class ForwarderTests(unittest.TestCase):
    def test_matches_either_target_and_whole_handle(self):
        self.assertTrue(mentioned(message("@ADMIN لطفاً"), CONFIG["target_usernames"]))
        for text in ("support", "@support_more", "mail@support.org", "@supportفارسی"):
            self.assertFalse(mentioned(message(text), CONFIG["target_usernames"]), text)

    def test_utf16_entity_after_emoji(self):
        msg = message("😀 @support")
        msg["entities"] = [{"type": "mention", "offset": 3, "length": 8}]
        self.assertTrue(mentioned(msg, CONFIG["target_usernames"]))

    def test_caption(self):
        msg = {"caption": "📷 @admin", "caption_entities": [
            {"type": "mention", "offset": 3, "length": 6}]}
        self.assertTrue(mentioned(msg, CONFIG["target_usernames"]))

    def test_other_entities_do_not_count_as_mentions(self):
        msg = message("a@support.com")
        msg["entities"] = [{"type": "email", "offset": 0, "length": 13}]
        self.assertFalse(mentioned(msg, CONFIG["target_usernames"]))

    def test_destination_private_and_source_filter(self):
        self.assertFalse(should_forward(message(chat_id=-99), CONFIG))
        msg = message()
        msg["chat"]["type"] = "private"
        self.assertFalse(should_forward(msg, CONFIG))
        config = dict(CONFIG, source_chat_ids=[-2])
        self.assertFalse(should_forward(message(), config))
        self.assertTrue(should_forward(message(chat_id=-2), config))

    def test_reply_parent_does_not_trigger(self):
        msg = message("باشه")
        msg["reply_to_message"] = message()
        self.assertFalse(should_forward(msg, CONFIG))

    def test_queue_deduplicates_edit_and_uses_original_message(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        store.ingest([{"update_id": 4, "message": message()},
                      {"update_id": 5, "edited_message": message()}], CONFIG)
        api = Mock()
        api.call.return_value = {"message_id": 42}
        store.forward_pending(api)
        store.forward_pending(api)
        self.assertEqual(store.offset(), 6)
        self.assertEqual(api.call.call_count, 2)
        self.assertEqual(api.call.call_args_list[0].args, ("forwardMessage",))
        self.assertEqual(api.call.call_args_list[0].kwargs,
                         {"chat_id": -99, "from_chat_id": -1, "message_id": 7})
        reply = api.call.call_args_list[1]
        self.assertEqual(reply.args, ("sendMessage",))
        self.assertEqual(reply.kwargs["reply_to_message_id"], 42)
        self.assertEqual(reply.kwargs["chat_id"], -99)
        self.assertIn("https://ble.ir/post/-1/7/1700000000000", reply.kwargs["text"])
        self.assertEqual(reply.kwargs["reply_markup"]["inline_keyboard"][0][0]["url"],
                         "https://ble.ir/post/-1/7/1700000000000")

    def test_failure_stays_queued_and_other_messages_continue(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        store.ingest([{"update_id": 4, "message": message()},
                      {"update_id": 5, "message": message(chat_id=-2)}], CONFIG)
        api = Mock()
        api.call.side_effect = [APIError(403, "Forbidden"), {"message_id": 42}, True]
        with self.assertLogs("bale-ticket", level="WARNING"):
            store.forward_pending(api)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM jobs WHERE done=0").fetchone()[0], 1)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM jobs WHERE done=1").fetchone()[0], 1)
        self.assertEqual(store.offset(), 6)

    def test_private_public_and_custom_source_links(self):
        msg = message()
        self.assertEqual(source_message_url(msg, CONFIG), "https://ble.ir/post/-1/7/1700000000000")
        msg["chat"]["username"] = "my_group"
        self.assertEqual(source_message_url(msg, CONFIG), "https://ble.ir/my_group/7/1700000000000")
        config = dict(CONFIG, source_link_templates={"-1": "https://ble.ir/example/{message_id}"})
        self.assertEqual(source_message_url(msg, config), "https://ble.ir/example/7")

    def test_failed_button_is_retried_without_reforwarding(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        store.ingest([{"update_id": 1, "message": message()}], CONFIG)
        api = Mock()
        api.call.side_effect = [{"message_id": 42}, APIError(500, "temporary"), True]
        with self.assertLogs("bale-ticket", level="WARNING"):
            store.forward_pending(api)
        store.db.execute("UPDATE jobs SET next_try=0")
        store.db.commit()
        store.forward_pending(api)
        self.assertEqual([call.args[0] for call in api.call.call_args_list],
                         ["forwardMessage", "sendMessage", "sendMessage"])
        self.assertEqual(store.db.execute("SELECT done FROM jobs").fetchone()[0], 1)

    def test_existing_pending_forward_gets_button_without_reforwarding(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        store.ingest([{"update_id": 1, "message": message()}], CONFIG)
        store.db.execute("UPDATE jobs SET forwarded_id=42")
        store.db.commit()
        api = Mock()
        api.call.return_value = {"message_id": 43}
        store.forward_pending(api)
        store.forward_pending(api)
        api.call.assert_called_once()
        self.assertEqual(api.call.call_args.args, ("sendMessage",))
        self.assertEqual(api.call.call_args.kwargs["reply_to_message_id"], 42)
        self.assertEqual(store.db.execute("SELECT done FROM jobs").fetchone()[0], 1)

    def test_failed_reply_stays_pending_and_logs_stage_and_error_code(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        store.ingest([{"update_id": 1, "message": message()}], CONFIG)
        api = Mock()
        api.call.side_effect = [{"message_id": 42}, APIError(403, "Forbidden")]
        with self.assertLogs("bale-ticket", level="WARNING") as logs:
            store.forward_pending(api)
        self.assertIn("Source button", logs.output[0])
        self.assertIn("API 403", logs.output[0])
        done, forwarded_id, next_try = store.db.execute(
            "SELECT done, forwarded_id, next_try FROM jobs").fetchone()
        self.assertEqual((done, forwarded_id), (0, 42))
        self.assertGreater(next_try, 0)


if __name__ == "__main__":
    unittest.main()
