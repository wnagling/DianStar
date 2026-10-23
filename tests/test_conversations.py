# 测试会话名称、旧格式兼容和按最近使用时间排序。

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from dianstar.conversations import Conversation, list_conversations


class ConversationNameTests(unittest.TestCase):
    def test_first_question_sets_six_character_name_and_rename_persists(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            conversation = Conversation.create(directory)
            self.assertIsNone(conversation.name)
            conversation.add_exchange("北京明天天气怎么样？", "晴。")
            self.assertEqual(conversation.name, "北京明天天气")
            conversation.rename("北京天气")
            conversation.add_exchange("再问一句", "好的。")
            restored = Conversation.load(conversation.id, directory)

        self.assertEqual(restored.name, "北京天气")
        self.assertEqual(len(restored.messages), 4)

    def test_legacy_conversation_uses_first_question_as_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            conversation = Conversation.create(directory)
            conversation.add_exchange("洛阳有哪些景点？", "龙门石窟。")
            path = directory / f"{conversation.id}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["version"] = 2
            del data["name"]
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            restored = Conversation.load(conversation.id, directory)
            self.assertEqual(restored.name, "洛阳有哪些景")
            restored.rename("河南旅行")
            upgraded = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(restored.name, "河南旅行")
        self.assertEqual(upgraded["version"], 3)
        self.assertEqual(upgraded["name"], "河南旅行")

    def test_version_one_conversation_can_be_named_on_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            conversation = Conversation.create(directory)
            conversation.add_exchange("你好，介绍一下", "你好。")
            path = directory / f"{conversation.id}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["version"] = 1
            del data["name"]
            del data["model_messages"]
            del data["model_context_start"]
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            restored = Conversation.load(conversation.id, directory)

        self.assertEqual(restored.name, "你好，介绍一")
        self.assertEqual(restored.model_context_messages(), restored.context_messages())

    def test_conversations_are_sorted_by_last_file_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            older = Conversation.create(directory)
            older.add_exchange("旧问题", "旧回答")
            newer = Conversation.create(directory)
            newer.add_exchange("新问题", "新回答")
            now = time.time()
            os.utime(directory / f"{older.id}.json", (now - 120, now - 120))
            os.utime(directory / f"{newer.id}.json", (now - 60, now - 60))
            entries = list_conversations(directory)

        self.assertEqual([entry.id for entry in entries], [newer.id, older.id])
        self.assertEqual([entry.name for entry in entries], ["新问题", "旧问题"])

    def test_blank_or_multiline_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            with self.assertRaises(ValueError):
                conversation.rename("   ")
            with self.assertRaises(ValueError):
                conversation.rename("bad\nname")
