import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.llm_provider import LLMCallCancelled
from core.prompt_loader import PromptLoader
from training.prose_editor import edit_chapter_prose


ORIGINAL = "他把欠条推回去。这说明他已经拒绝了这笔交易。"
REPLACEMENT = "他把欠条推回去。"
PROSE = "第1章 欠条\r\n\r\n  " + ORIGINAL + "  \r\n\r\n掌柜没接，手还压在账本上。\r\n"


def issue(index=1, quote="这说明他已经拒绝了这笔交易。", **overrides):
    result = {"index": index, "quote": quote, "category": "redundant_explanation",
              "scope": "local", "reason": "推回欠条已表明拒绝，后句重复解释。",
              "instruction": "保留动作，删除紧随其后的重复解释。"}
    result.update(overrides)
    return result


def replacement(index=1, original=ORIGINAL, text=REPLACEMENT):
    return {"index": index, "original": original, "text": text}


def check(index=1, **overrides):
    result = {"index": index, "facts_preserved": True, "issue_resolved": True,
              "no_new_problems": True, "reason": "拒绝的动作与结果保留，删去了重复解释，后文仍能承接。"}
    result.update(overrides)
    return result


class ScriptedLLM:
    model = "test-editor"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.cancelable_calls = 0

    def generate(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    def generate_cancelable(self, prompt, cancel_event, **kwargs):
        self.cancelable_calls += 1
        return self.generate(prompt, **kwargs)


class ProseEditorTests(unittest.TestCase):
    def setUp(self):
        PromptLoader._prompts.clear()

    def test_accepts_evidenced_edit_and_preserves_exact_surrounding_format(self):
        llm = ScriptedLLM({"issues": [issue()]}, {"replacements": [replacement()]}, {"checks": [check()]})
        result, report = edit_chapter_prose(
            llm, PROSE, chapter_outline="他拒绝掌柜的交易。", story_arc="双方尚未谈妥。",
            recent_context="上一章仅见过掌柜，没有借款。", style_anchor="文风样本哨兵",
        )
        self.assertEqual(result, PROSE.replace(ORIGINAL, REPLACEMENT))
        self.assertEqual(report["status"], "edited")
        self.assertEqual(len(report["applied_edits"]), 1)
        self.assertNotEqual(report["input_sha256"], report["output_sha256"])
        self.assertEqual(len(llm.calls), 3)
        self.assertTrue(all(kwargs["is_json"] for _, kwargs in llm.calls))
        for prompt, _ in llm.calls:
            self.assertIn("他拒绝掌柜的交易。", prompt)
            self.assertIn("文风样本哨兵", prompt)

    def test_no_issue_is_one_call_and_exact_noop(self):
        llm = ScriptedLLM({"issues": []})
        result, report = edit_chapter_prose(llm, PROSE)
        self.assertEqual(result, PROSE)
        self.assertEqual(report["status"], "no_changes")
        self.assertEqual(len(llm.calls), 1)

    def test_single_newline_body_is_reviewable_without_editing_title(self):
        text = "第1章 欠条\n" + ORIGINAL + "\n掌柜没接。"
        llm = ScriptedLLM({"issues": [issue(), issue(0, "第1章 欠条")]},
                          {"replacements": [replacement()]}, {"checks": [check()]})
        result, report = edit_chapter_prose(llm, text)
        self.assertEqual(result, text.replace(ORIGINAL, REPLACEMENT))
        self.assertTrue(any(item["index"] == 0 for item in report["rejected"]))

    def test_first_paragraph_without_title_can_be_edited(self):
        llm = ScriptedLLM({"issues": [issue(0)]}, {"replacements": [replacement(0)]}, {"checks": [check(0)]})
        result, _ = edit_chapter_prose(llm, ORIGINAL + "\n\n掌柜没接。")
        self.assertEqual(result, REPLACEMENT + "\n\n掌柜没接。")

    def test_planning_issue_is_recorded_and_never_rewritten(self):
        llm = ScriptedLLM({"issues": [issue(scope="planning", category="repeated_beat"), issue()]})
        result, report = edit_chapter_prose(llm, PROSE)
        self.assertEqual(result, PROSE)
        self.assertEqual(len(report["planning_issues"]), 1)
        self.assertEqual(len(llm.calls), 1)

    def test_malformed_or_unanchored_issues_do_not_trigger_edits(self):
        llm = ScriptedLLM({"issues": [issue(quote="不存在的原句"), issue(index=True),
                                      issue(category=[]), issue(scope={}), issue(index=999)]})
        result, report = edit_chapter_prose(llm, PROSE)
        self.assertEqual(result, PROSE)
        self.assertEqual(len(report["rejected"]), 5)
        self.assertEqual(len(llm.calls), 1)

    def test_wrong_original_unrequested_and_duplicate_edits_are_rejected(self):
        for proposals in (
            [replacement(original="另一个段落")], [replacement(index=2)],
            [replacement(), replacement()], [replacement(text="改成第一段。\n\n另起一段。")],
        ):
            with self.subTest(proposals=proposals):
                llm = ScriptedLLM({"issues": [issue()]}, {"replacements": proposals})
                result, report = edit_chapter_prose(llm, PROSE)
                self.assertEqual(result, PROSE)
                self.assertEqual(report["applied_edits"], [])
                self.assertEqual(len(llm.calls), 2)

    def test_quantity_changes_are_rejected_before_model_verification(self):
        for original, revised in (("他递过3枚铜钱。", "他递过5枚铜钱。"),
                                  ("他已在这里住了三年。", "他已在这里住了五年。")):
            with self.subTest(original=original):
                llm = ScriptedLLM({"issues": [issue(quote=original)]},
                                  {"replacements": [replacement(original=original, text=revised)]})
                text = "第1章\n\n" + original
                result, report = edit_chapter_prose(llm, text)
                self.assertEqual(result, text)
                self.assertEqual(report["rejected"][0]["reason"], "quantity_changed")
                self.assertEqual(len(llm.calls), 2)

    def test_semantic_fact_change_or_no_improvement_is_not_accepted(self):
        for checks in ([check(facts_preserved=False)], [check(issue_resolved=False)],
                       [check(no_new_problems=False)], [check(facts_preserved="true")],
                       [check(reason="")], [], [check(), check(facts_preserved=False)]):
            with self.subTest(checks=checks):
                llm = ScriptedLLM({"issues": [issue()]}, {"replacements": [replacement()]}, {"checks": checks})
                result, report = edit_chapter_prose(llm, PROSE)
                self.assertEqual(result, PROSE)
                self.assertFalse(report["applied_edits"])

    def test_failure_at_any_model_stage_keeps_exact_original_and_reports_failure(self):
        stages = [[], [{"issues": [issue()]}], [{"issues": [issue()]}, {"replacements": [replacement()]}]]
        for prior in stages:
            for failure in (RuntimeError("provider secret must not be persisted"), "not json", {}):
                with self.subTest(stage=len(prior), failure=failure):
                    llm = ScriptedLLM(*prior, failure)
                    result, report = edit_chapter_prose(llm, PROSE)
                    self.assertEqual(result, PROSE)
                    self.assertTrue(report["status"].endswith("failed"))
                    self.assertNotIn("provider secret", json.dumps(report))

    def test_cancellation_propagates_at_every_stage(self):
        stages = [[], [{"issues": [issue()]}], [{"issues": [issue()]}, {"replacements": [replacement()]}]]
        for prior in stages:
            with self.subTest(stage=len(prior)):
                event = threading.Event()
                llm = ScriptedLLM(*prior, LLMCallCancelled("stop"))
                with self.assertRaises(LLMCallCancelled):
                    edit_chapter_prose(llm, PROSE, cancel_event=event)
                self.assertEqual(llm.cancelable_calls, len(prior) + 1)
        event.set()
        llm = ScriptedLLM()
        with self.assertRaises(LLMCallCancelled):
            edit_chapter_prose(llm, PROSE, cancel_event=event)
        self.assertFalse(llm.calls)

    def test_cancellation_after_verification_response_prevents_returning_edits(self):
        event = threading.Event()
        llm = ScriptedLLM({"issues": [issue()]}, {"replacements": [replacement()]}, {"checks": [check()]})
        generate = llm.generate

        def cancel_after_verify(prompt, **kwargs):
            value = generate(prompt, **kwargs)
            if len(llm.calls) == 3:
                event.set()
            return value

        llm.generate = cancel_after_verify
        with self.assertRaises(LLMCallCancelled):
            edit_chapter_prose(llm, PROSE, cancel_event=event)

    def test_oversized_factual_context_keeps_original_without_request(self):
        llm = ScriptedLLM()
        result, report = edit_chapter_prose(llm, PROSE, chapter_outline="事实" * 31000)
        self.assertEqual(result, PROSE)
        self.assertEqual(report["reason"], "context_limit")
        self.assertFalse(llm.calls)

    def test_template_errors_are_not_silently_reported_as_no_issues(self):
        with patch.object(PromptLoader, "load", side_effect=KeyError("broken template")):
            with self.assertRaises(KeyError):
                edit_chapter_prose(ScriptedLLM(), PROSE)

    def test_edit_count_is_bounded_and_rejected_edits_leave_neighbours_intact(self):
        text = "第1章 欠条\n\n" + "\n\n".join([ORIGINAL] * 10)
        llm = ScriptedLLM(
            {"issues": [issue(index=i) for i in range(1, 11)]},
            {"replacements": [replacement(index=i) for i in range(1, 11)]},
            {"checks": [check(1), check(2, facts_preserved=False)]},
        )
        result, report = edit_chapter_prose(llm, text, strength="light")
        self.assertEqual(report["max_edits"], 2)
        self.assertEqual(result, text.replace(ORIGINAL, REPLACEMENT, 1))
        self.assertEqual(len(report["applied_edits"]), 1)

    def test_drafting_wrapper_keeps_raw_backup_and_persists_edit_evidence(self):
        from training import adaptive_builder as drafting

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ws = SimpleNamespace(file_system=str(root / "file_system"), reference=str(root / "reference"))
            text = PROSE.replace("\r\n", "\n")
            llm = ScriptedLLM({"issues": [issue()]}, {"replacements": [replacement()]}, {"checks": [check()]})
            with patch.object(drafting, "human_style_library_status", return_value={"ready": True}):
                result = drafting._humanize_chapter_text(
                    llm, ws, 1, 1, text, style_anchor="人工风格依据", chapter_outline="拒绝交易。",
                )
            self.assertEqual(result, text.replace(ORIGINAL, REPLACEMENT))
            draft_dir = root / "file_system/drafts/vol_01"
            self.assertEqual((draft_dir / "raw_chapters/001_第1章.raw.md").read_text(encoding="utf-8").strip(), text.strip())
            paths = list((draft_dir / "editor_reviews").glob("*.json"))
            self.assertEqual(len(paths), 1)
            report = json.loads(paths[0].read_text(encoding="utf-8"))
            self.assertEqual(report["chapter"], 1)
            self.assertEqual(report["status"], "edited")
            self.assertEqual(report["applied_edits"][0]["original"], ORIGINAL)


if __name__ == "__main__":
    unittest.main()
