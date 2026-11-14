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
        button = reply.kwargs["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button, {"text": "جواب به این پیام", "callback_data": "reply:42"})

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

    def test_destination_reply_is_sent_to_the_matching_original(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        store.ingest([{"update_id": 1, "message": message()},
                      {"update_id": 2, "message": message(chat_id=-2)}], CONFIG)
        store.db.execute("UPDATE jobs SET forwarded_id=42 WHERE source=-1")
        store.db.execute("UPDATE jobs SET forwarded_id=43 WHERE source=-2")
        store.db.commit()
        reply = {"message_id": 100, "chat": {"id": -99, "type": "group"},
                 "text": "پاسخ دوم", "reply_to_message": {"message_id": 43}}
        store.ingest([{"update_id": 3, "message": reply},
                      {"update_id": 4, "edited_message": reply}], CONFIG)
        api = Mock()
        store.send_pending_replies(api)
        store.send_pending_replies(api)
        api.call.assert_called_once_with("sendMessage", chat_id=-2, reply_to_message_id=7, text="پاسخ دوم")

    def test_unmatched_and_nontext_replies_are_not_sent(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        store.ingest([{"update_id": 1, "message": message()}], CONFIG)
        store.db.execute("UPDATE jobs SET forwarded_id=42")
        store.db.commit()
        for update_id, replied_id, content in ((2, 999, {"text": "نامرتبط"}), (3, 42, {"photo": []})):
            store.ingest([{"update_id": update_id, "message": {
                "message_id": update_id, "chat": {"id": -99},
                "reply_to_message": {"message_id": replied_id}, **content}}], CONFIG)
        api = Mock()
        store.send_pending_replies(api)
        api.call.assert_not_called()

    def test_failed_destination_reply_is_retried(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        store.ingest([{"update_id": 1, "message": message()}], CONFIG)
        store.db.execute("UPDATE jobs SET forwarded_id=42")
        store.db.commit()
        store.ingest([{"update_id": 2, "message": {"message_id": 100,
                      "chat": {"id": -99}, "text": "پاسخ",
                      "reply_to_message": {"message_id": 42}}}], CONFIG)
        api = Mock()
        api.call.side_effect = [APIError(500, "temporary"), True]
        with self.assertLogs("bale-ticket", level="WARNING"):
            store.send_pending_replies(api)
        store.db.execute("UPDATE replies SET next_try=0")
        store.db.commit()
        store.send_pending_replies(api)
        self.assertEqual(api.call.call_count, 2)
        self.assertEqual(store.db.execute("SELECT done FROM replies").fetchone()[0], 1)

    def test_button_selection_routes_each_users_next_message(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        store.ingest([{"update_id": 1, "message": message()},
                      {"update_id": 2, "message": message(chat_id=-2)}], CONFIG)
        store.db.execute("UPDATE jobs SET forwarded_id=42 WHERE source=-1")
        store.db.execute("UPDATE jobs SET forwarded_id=43 WHERE source=-2")
        store.db.commit()
        updates = [
            {"update_id": 3, "callback_query": {"id": "a", "from": {"id": 1},
             "message": {"chat": {"id": -99}}, "data": "reply:42"}},
            {"update_id": 4, "callback_query": {"id": "b", "from": {"id": 2},
             "message": {"chat": {"id": -99}}, "data": "reply:43"}},
            {"update_id": 5, "message": {"message_id": 50, "chat": {"id": -99},
             "from": {"id": 2}, "text": "دوم"}},
            {"update_id": 6, "message": {"message_id": 51, "chat": {"id": -99},
             "from": {"id": 1}, "text": "اول"}},
            {"update_id": 7, "message": {"message_id": 52, "chat": {"id": -99},
             "from": {"id": 1}, "text": "پیام عادی"}},
        ]
        store.ingest(updates, CONFIG)
        api = Mock()
        store.send_pending_replies(api)
        self.assertEqual([call.kwargs for call in api.call.call_args_list], [
            {"chat_id": -2, "reply_to_message_id": 7, "text": "دوم"},
            {"chat_id": -1, "reply_to_message_id": 7, "text": "اول"},
        ])
        store.answer_callbacks(api)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM callback_answers").fetchone()[0], 0)

    def test_cancel_discards_button_selection(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        store.ingest([{"update_id": 1, "message": message()}], CONFIG)
        store.db.execute("UPDATE jobs SET forwarded_id=42")
        store.db.commit()
        store.ingest([
            {"update_id": 2, "callback_query": {"id": "a", "from": {"id": 1},
             "message": {"chat": {"id": -99}}, "data": "reply:42"}},
            {"update_id": 3, "message": {"message_id": 3, "chat": {"id": -99},
             "from": {"id": 1}, "text": "/cancel"}},
            {"update_id": 4, "message": {"message_id": 4, "chat": {"id": -99},
             "from": {"id": 1}, "text": "پیام عادی"}},
        ], CONFIG)
        api = Mock()
        store.send_pending_replies(api)
        api.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
