"""POST /questions/generate with mode "function". The LLM and DB are faked; the Judge0 fake
runs the Python source it is sent (reference function + the real driver) in a local
subprocess, so the expected outputs here really come from reference + driver."""

import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.execution.coding_questions import generate as gen
from app.execution.function_mode import values
from app.execution.function_mode.signature import Signature
from app.execution.judge0 import ACCEPTED, RunResult
from tests.execution import test_generate_coding as tg

db = tg.db  # the faked coding-question repo (fixture)

SIGNATURE = {
    "function_name": "maxPairSum",
    "parameters": [{"name": "nums", "type": "int[]"}, {"name": "limit", "type": "long"}],
    "return_type": "long",
}
REFERENCE = (
    "def maxPairSum(nums, limit):\n"
    "    best = -1\n"
    "    for i in range(len(nums)):\n"
    "        for j in range(i + 1, len(nums)):\n"
    "            if nums[i] + nums[j] <= limit:\n"
    "                best = max(best, nums[i] + nums[j])\n"
    "    return best\n"
)


def reference_answer(nums, limit):
    s = [a + b for i, a in enumerate(nums) for b in nums[i + 1 :] if a + b <= limit]
    return max(s, default=-1)


INPUTS = [{"nums": [i, i + 2, 5], "limit": 3 + i} for i in range(18)]


def reply(**over) -> dict:
    return {
        "title": "Best Pair Under a Limit",
        "statement": "Find the largest pair sum not above limit.\nParameters: nums, limit.\n"
        "Returns: the sum, or -1.",
        "signature": SIGNATURE,
        "reference_solution": REFERENCE,
        "sample_inputs": INPUTS[:3],
        "hidden_inputs": INPUTS[3:],
        **over,
    }


class LocalJudge0:
    """Runs Python sources locally, one subprocess per stdin (unit tests never call Judge0)."""

    def __init__(self):
        self.calls: list[tuple[str, str, list[str]]] = []

    async def run_many(self, language, source, stdins):
        assert language == "python"
        self.calls.append((language, source, stdins))
        out = []
        for stdin in stdins:
            p = subprocess.run(
                [sys.executable, "-I", "-c", source],
                input=stdin.encode(),
                capture_output=True,
                timeout=20,
            )
            if p.returncode == 0:
                out.append(
                    RunResult(ACCEPTED, "Accepted", p.stdout.decode(), "", "", "", 0.01, 1, 0)
                )
            else:
                err = p.stderr.decode()
                out.append(RunResult(11, "Runtime Error (NZEC)", "", err, "", "", 0.01, 1, 1))
        return out


class RecordingLlm(tg.FakeLlm):
    def __init__(self, script):
        super().__init__(script)
        self.systems: list[str] = []

    async def complete_json(self, *, system: str, user: str) -> dict:
        self.systems.append(system)
        return await super().complete_json(system=system, user=user)


def test_function_question_is_saved_with_signature_and_expected_from_reference_plus_driver(db):
    llm = RecordingLlm({0: [reply()]})
    judge0 = LocalJudge0()
    c, _ = tg.use(llm, judge0)
    r = tg.post(c, mode="function", topic="arrays")
    assert r.status_code == 201, r.text
    [q] = r.json()["created"]
    assert q["mode"] == "function" and q["signature"] == SIGNATURE and q["approved"] is False
    assert q["reference_solution"] == REFERENCE.strip("\n") and q["reference_language"] == "python"
    sig = Signature.model_validate(SIGNATURE)
    for case, args in zip(q["test_cases"], INPUTS, strict=True):
        assert case["input"] == values.canonical_args(sig, args)  # canonical, one line each
        assert case["expected"] == f"{reference_answer(**args)}\n"
    assert [x["is_sample"] for x in q["test_cases"]] == [True] * 3 + [False] * 15
    [(_, source, stdins)] = judge0.calls
    assert source.startswith(REFERENCE) and "_drv_main()" in source and len(stdins) == 18
    assert llm.systems == [gen.FUNCTION_SYSTEM_PROMPT]
    assert "_drv" not in r.text  # the driver never appears in the response
    assert db.questions[next(iter(db.questions))]["signature"] == SIGNATURE


def test_stdin_mode_still_uses_the_stdin_prompt(db):
    llm = RecordingLlm({0: [tg.reply()]})
    c, _ = tg.use(llm)
    r = tg.post(c)
    assert r.status_code == 201 and r.json()["created"][0]["mode"] == "stdin"
    assert llm.systems == [gen.SYSTEM_PROMPT]


def test_a_reference_returning_the_wrong_type_is_retried_with_the_drivers_message(db):
    wrong = REFERENCE.replace("    return best\n", "    return str(best)\n")
    llm = RecordingLlm({0: [reply(reference_solution=wrong), reply()]})
    c, _ = tg.use(llm, LocalJudge0())
    r = tg.post(c, mode="function")
    assert r.status_code == 201 and len(r.json()["created"]) == 1
    assert "maxPairSum returned '" in llm.prompts[0][1] and "must return long" in llm.prompts[0][1]


def test_invalid_function_replies_fail_after_one_retry(db):
    bad = reply(signature={**SIGNATURE, "return_type": "float"})
    llm = RecordingLlm({0: [bad, bad]})
    c, j = tg.use(llm, LocalJudge0())
    r = tg.post(c, mode="function")
    assert r.status_code == 502 and r.json()["error"]["code"] == "AI_BAD_OUTPUT"
    assert "signature.return_type" in r.text and j.calls == [] and db.questions == {}


@pytest.mark.parametrize(
    "over,why",
    [
        ({"signature": {**SIGNATURE, "function_name": "solve"}}, "function_name"),
        ({"signature": {**SIGNATURE, "parameters": [{"name": "int", "type": "int"}]}}, "reserved"),
        ({"signature": {**SIGNATURE, "extra": 1}}, "signature"),
        ({"sample_inputs": [{"nums": [1]}, *INPUTS[1:3]]}, "keys"),
        ({"sample_inputs": [{"nums": [1], "limit": 1, "x": 2}, *INPUTS[1:3]]}, "keys"),
        ({"sample_inputs": [{"nums": [1, "2"], "limit": 1}, *INPUTS[1:3]]}, "int"),
        ({"sample_inputs": [{"nums": [1], "limit": 2**60}, *INPUTS[1:3]]}, "long"),
        ({"sample_inputs": ["[1]\n2\n", *INPUTS[1:3]]}, "dict"),
        ({"hidden_inputs": [INPUTS[0]] * 15}, "different"),
        ({"statement": "Read n from standard input."}, "stdin"),
        ({"reference_solution": "class Solution:\n    pass\n"}, "top-level function"),
        ({"reference_solution": "def maxPairSum(nums):\n    return 1\n"}, "parameters"),
        ({"reference_solution": "def maxPairSum(*a):\n    return 1\n"}, "parameters"),
        (
            {"reference_solution": "def maxPairSum(nums, limit):\n    return int(input())\n"},
            "input",
        ),
        (
            {"reference_solution": "import sys\ndef maxPairSum(a, b):\n    sys.stdin.read()\n"},
            "stdin",
        ),
        ({"reference_solution": "def maxPairSum(nums, limit:\n"}, "not valid Python"),
    ],
)
def test_function_reply_schema_is_strict(over, why):
    with pytest.raises(ValidationError) as e:
        gen.GeneratedFunctionQuestion.model_validate(reply(**over))
    assert why in str(e.value)


def test_a_valid_reply_keeps_canonical_inputs_and_ignores_extra_fields():
    q = gen.GeneratedFunctionQuestion.model_validate(reply(chatter="hi"))
    assert q.inputs[0] == "[0,2,5]\n3\n" and q.mode == "function" and len(q.inputs) == 18
