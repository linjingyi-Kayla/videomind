from __future__ import annotations

import json
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from videomind.agent.agent import run_agent
from videomind.agent.state import ToolContext
from videomind.agent.tools import execute_tool, save_note, set_reminder
from videomind.agent.transcript import search_cues
from videomind.agent.web_search import web_search
from videomind.db_models import Base, Task, User
from videomind.remind import parse_remind_at

SAMPLE_SUBS = """
[04:50] 微软早期押宝 OpenAI，前后投了大约一百三十亿美元，占股约百分之二十七。
[04:54] 微软是 OpenAI 最核心的外部投资人之一。
[05:10] 软银孙正义投了超过六百亿美元，拿到约百分之十三股权。
[05:13] 亚马逊也承诺最多投五百亿，成为核心外部投资人之一。
[07:47] OpenAI 跟英伟达签了三百亿美元战略合作意向书，用 GPU 换投资。
[11:53] 星际之门原本是史上最大数据中心项目，但融资不顺、内部争吵。
[12:00] OpenAI 内部已实质性放弃这个大型联合项目，转向双边合同。
[14:00] 不想只依赖英伟达，跟博通合作自研芯片，并与 AMD 谈下大单。
[14:20] AMD 提供六吉瓦算力，同时给 OpenAI 发行最多一点六亿股低价权证。
[14:40] 相当于用股权换订单，分散对单一供应商的依赖。
""".strip()


class Fn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class ToolCall:
    def __init__(self, cid: str, name: str, arguments: dict) -> None:
        self.id = cid
        self.type = "function"
        self.function = Fn(name, json.dumps(arguments, ensure_ascii=False))


class Msg:
    def __init__(self, content=None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class Resp:
    def __init__(self, message: Msg) -> None:
        self.choices = [SimpleNamespace(message=message)]


class ScriptClient:
    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            return Resp(Msg(content="（测试兜底）"))
        item = self._responses.pop(0)
        return item if isinstance(item, Resp) else Resp(item)


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    user = User(email="u@example.com", hashed_password="x", created_at=datetime.utcnow())
    s.add(user)
    s.commit()
    s.refresh(user)
    task = Task(
        task_uuid="task-demo",
        video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        title="OpenAI 资本操作",
        category="投资理财",
        summary="OpenAI 靠微软、软银、亚马逊融资，并用订单绑定英伟达与 AMD。",
        key_points_json=json.dumps(["微软持股", "Stargate 受阻", "AMD 权证"], ensure_ascii=False),
        status="done",
        user_id=user.id,
        subtitles_text=SAMPLE_SUBS,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    s.add(task)
    s.commit()
    return s, user, task


class TranscriptSearchTests(unittest.TestCase):
    def test_finds_stargate_window(self):
        out = search_cues(SAMPLE_SUBS, "OpenAI 放弃 星际之门 Stargate", top_k=3)
        self.assertTrue(out["success"])
        self.assertTrue(out["results"])
        blob = " ".join(r["text"] for r in out["results"])
        self.assertTrue("放弃" in blob or "星际之门" in blob)

    def test_finds_amd_window(self):
        out = search_cues(SAMPLE_SUBS, "AMD 权证", top_k=2)
        self.assertTrue(out["success"])
        blob = " ".join(r["text"] for r in out["results"])
        self.assertIn("AMD", blob)


class WebSearchConfigTests(unittest.TestCase):
    def test_not_configured(self):
        env = {k: v for k, v in os.environ.items() if k not in ("WEB_SEARCH_API_KEY", "WEB_SEARCH_PROVIDER")}
        with patch.dict(os.environ, env, clear=True):
            out = web_search("OpenAI Stargate latest status")
        self.assertFalse(out["success"])
        self.assertEqual(out["error"], "web_search_not_configured")


class ToolOwnershipTests(unittest.TestCase):
    def test_save_note_and_reminder(self):
        s, user, task = _session()
        ctx = ToolContext(user_id=user.id, task_id=task.task_uuid, tz_offset_minutes=-480, session=s)
        note = save_note(ctx, {"content": "AMD 权证换订单", "source_timestamp": "14:00-14:44"})
        self.assertTrue(note["success"])
        self.assertIn("AMD", note["annotation"])

        other = ToolContext(user_id=user.id + 99, task_id=task.task_uuid, session=s)
        denied = save_note(other, {"content": "hack"})
        self.assertFalse(denied["success"])
        self.assertEqual(denied["error"], "task_not_found")

        rem = set_reminder(ctx, {"remind_at": "明晚", "content": "复习 AMD"})
        self.assertTrue(rem["success"])
        s.refresh(task)
        self.assertIsNotNone(task.remind_at)
        self.assertFalse(task.is_notified)
        s.close()

    def test_mismatch_task_id(self):
        s, user, task = _session()
        ctx = ToolContext(user_id=user.id, task_id=task.task_uuid, session=s)
        out = execute_tool(ctx, "get_video_summary", {"task_id": "other"})
        self.assertEqual(out["error"], "task_id_mismatch")
        s.close()


class AgentLoopDemoTests(unittest.TestCase):
    def _run(self, script, message: str):
        s, user, task = _session()
        client = ScriptClient(script)
        with patch("videomind.agent.agent.execute_tool") as ex:

            def _real(ctx, name, args):
                ctx.session = s
                return execute_tool(ctx, name, args)

            ex.side_effect = _real
            result = run_agent(
                user_id=user.id,
                task_id=task.task_uuid,
                user_message=message,
                chat_history=[],
                tz_offset_minutes=-480,
                client=client,
            )
        names = []
        for call in client.calls:
            # tool names inferred from script consumption; inspect last assistant tool traces via result
            pass
        s.close()
        return result, client

    def test_demo1_transcript_then_answer(self):
        script = [
            Msg(tool_calls=[ToolCall("c1", "search_transcript", {"query": "OpenAI 怎么做 融资"})]),
            Msg(content="根据视频：微软、软银、亚马逊输血，并用订单绑定芯片方 [04:54-05:13]。"),
        ]
        result, client = self._run(script, "OpenAI具体是怎么做的？")
        self.assertIn("根据视频", result.content)
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(result.traces[0]["tool"], "search_transcript")
        self.assertNotIn("web_search", [t["tool"] for t in result.traces])

    def test_demo2_multi_search(self):
        script = [
            Msg(tool_calls=[ToolCall("c1", "search_transcript", {"query": "英伟达 投资"})]),
            Msg(tool_calls=[ToolCall("c2", "search_transcript", {"query": "AMD 博通"})]),
            Msg(content="根据视频：一边拿英伟达 GPU 换投资 [07:47]，一边找 AMD/博通分散风险 [14:00]。"),
        ]
        result, _ = self._run(script, "为什么OpenAI一边接受英伟达投资，一边又去找AMD和博通？")
        self.assertEqual(result.tool_call_count, 2)
        self.assertEqual([t["tool"] for t in result.traces], ["search_transcript", "search_transcript"])

    def test_demo3_verify_then_web(self):
        script = [
            Msg(tool_calls=[ToolCall("c1", "search_transcript", {"query": "放弃 星际之门 Stargate"})]),
            Msg(tool_calls=[ToolCall("c2", "web_search", {"query": "OpenAI Stargate latest status"})]),
            Msg(content="根据视频：内部已实质性放弃联合项目 [11:53-12:00]。\n外部公开信息：搜索未配置，无法核对最新公开进展。"),
        ]
        result, _ = self._run(script, "视频说OpenAI已经放弃Stargate，这是真的吗？")
        self.assertEqual([t["tool"] for t in result.traces], ["search_transcript", "web_search"])
        self.assertIn("根据视频", result.content)
        self.assertIn("外部公开信息", result.content)

    def test_demo4_web_only(self):
        script = [
            Msg(tool_calls=[ToolCall("c1", "web_search", {"query": "Stargate project now"})]),
            Msg(content="外部公开信息：当前未配置搜索密钥，无法查询最新进展。"),
        ]
        result, _ = self._run(script, "现在Stargate项目怎么样了？")
        self.assertEqual([t["tool"] for t in result.traces], ["web_search"])
        self.assertNotIn("search_transcript", [t["tool"] for t in result.traces])

    def test_demo5_save_note(self):
        script = [
            Msg(tool_calls=[ToolCall("c1", "search_transcript", {"query": "AMD"})]),
            Msg(
                tool_calls=[
                    ToolCall(
                        "c2",
                        "save_note",
                        {"content": "AMD 用算力换权证", "source_timestamp": "14:00-14:44"},
                    )
                ]
            ),
            Msg(content="已把 AMD 那段记进批注 [14:00-14:44]。"),
        ]
        result, _ = self._run(script, "把刚才AMD那段记下来。")
        self.assertEqual([t["tool"] for t in result.traces], ["search_transcript", "save_note"])
        self.assertTrue(result.traces[1]["success"])

    def test_demo6_note_and_reminder(self):
        script = [
            Msg(
                tool_calls=[
                    ToolCall("c1", "save_note", {"content": "结论：众星捧月融资，分散芯片依赖"}),
                    ToolCall("c2", "set_reminder", {"remind_at": "明晚", "content": "复习该结论"}),
                ]
            ),
            Msg(content="已记下结论，并设好明晚复习提醒。"),
        ]
        result, _ = self._run(script, "把这个结论记下来，明晚提醒我复习。")
        self.assertEqual([t["tool"] for t in result.traces], ["save_note", "set_reminder"])
        self.assertTrue(all(t["success"] for t in result.traces))


class RemindParseTests(unittest.TestCase):
    def test_hhmm_and_tomorrow_night(self):
        a = parse_remind_at("18:30", -480)
        b = parse_remind_at("明晚", -480)
        self.assertEqual(a.minute, 30)
        self.assertIsNotNone(b)


if __name__ == "__main__":
    unittest.main()
