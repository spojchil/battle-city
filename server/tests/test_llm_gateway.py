import json

from linx_game.llm_gateway import _empty_reply, _strip_content, strip_platform_prompt


def _sse(*deltas: dict) -> bytes:
    lines = [f"data: {json.dumps({'choices': [{'index': 0, 'delta': d}]}, ensure_ascii=False)}" for d in deltas]
    return ("\n\n".join([*lines, "data: [DONE]"]) + "\n\n").encode()


def test_strip_content_keeps_tool_calls():
    raw = _sse(
        {"role": "assistant", "content": "他在撒谎，"},
        {"content": "先稳住他。"},
        {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "say", "arguments": "{}"}}]},
    )
    thought, stripped = _strip_content(raw)
    assert thought == "他在撒谎，先稳住他。"
    text = stripped.decode()
    assert "他在撒谎" not in text
    assert '"name": "say"' in text
    assert text.rstrip().endswith("data: [DONE]")


def test_strip_content_plain_json():
    raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": "想法"}}]}).encode()
    thought, stripped = _strip_content(raw)
    assert thought == "想法"
    assert json.loads(stripped)["choices"][0]["message"]["content"] == ""


def test_empty_reply_stream():
    resp = _empty_reply({"model": "m", "stream": True})
    body = resp.body.decode()
    assert body.startswith("data: ") and body.rstrip().endswith("data: [DONE]")
    chunk = json.loads(body.split("\n")[0].removeprefix("data: "))
    assert chunk["choices"][0]["finish_reason"] == "stop"


def test_strip_platform_prompt():
    system = "默认回复语言是中文角色设定\n# 终端用户上下文\n位置：上海"
    assert strip_platform_prompt(system) == "角色设定"
