"""按剧本批量开通灵矽智能体与设备。

每个 AI 角色对应一个智能体（音色取剧本中的 voice），并开通两台虚拟设备：
brain 用于接收感知、调用模型与工具；mouth 只用 text_response 播报台词。
玩家对应一个智能体与一台 ear 设备，仅用于语音识别。

已开通的智能体与设备记录在 PROVISION_STATE（默认 ~/.config/linx-game/provision.json），
重复运行只补齐缺失部分，并把智能体配置更新为当前剧本。
"""

import argparse
import json
import os
import random
import uuid
from pathlib import Path

import httpx
import yaml
from dotenv import find_dotenv, load_dotenv

from linx_game.devtools.vdevice import ota

STATE_PATH = Path(os.getenv("PROVISION_STATE", Path.home() / ".config" / "linx-game" / "provision.json"))

# 平台内置工具与游戏无关，且会干扰模型选择工具
DISABLED_BUILTIN_TOOLS = ["get_time", "get_lunar", "handle_exit_intent"]

# 实际提示词由大模型网关按游戏状态生成，智能体上只保留说明
AGENT_PROMPT = "你在操控游戏角色「{name}」（{role}）。完整设定与局势由游戏服务器提供。"


class Linx:
    def __init__(self):
        self.client = httpx.Client(
            base_url=os.environ["LINX_BASE_URL"], timeout=20,
            headers={"Authorization": f"Bearer {os.environ['LINX_API_KEY']}"},
        )

    def call(self, method: str, path: str, **kwargs) -> dict:
        resp = self.client.request(method, path, **kwargs)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") not in (0, None):
            raise RuntimeError(f"{method} {path} 失败：{body}")
        return body.get("data")


def ensure_agent(linx: Linx, state: dict, key: str, name: str, role: str, voice: str, mcp: bool) -> str:
    config = {
        "agentName": key,
        "assistantName": name,
        "systemPrompt": AGENT_PROMPT.format(name=name, role=role),
        "llmModelId": os.environ["LINX_LLM_MODEL_ID"],
        "memModelId": "Memory_nomem",
        "intentModelId": "Intent_function_call",
        "langCode": "zh",
    }
    if voice:
        config["ttsVoiceId"] = voice
    agent_id = state["agents"].get(key)
    if agent_id is None:
        agent_id = linx.call("POST", "/xiaozhi/agent", json=config)
        agent_id = agent_id["id"] if isinstance(agent_id, dict) else agent_id
        state["agents"][key] = agent_id
        print(f"创建智能体 {key} -> {agent_id}")
    linx.call("PUT", f"/xiaozhi/agent/{agent_id}", json={
        **config, "functions": [], "extra": {"disabled_builtin_tools": DISABLED_BUILTIN_TOOLS},
    })
    mcp_ids = [os.environ["LINX_MCP_ID"]] if mcp else []
    linx.call("PUT", f"/v1/agents/{agent_id}/mcps", json={"mcp_ids": mcp_ids})
    return agent_id


def ensure_device(linx: Linx, state: dict, key: str, agent_id: str) -> dict:
    ident = state["devices"].get(key)
    if ident is None:
        # 本地管理地址（首字节 0x02），不与真实网卡冲突
        mac = "02:" + ":".join(f"{random.randint(0, 255):02X}" for _ in range(5))
        ident = state["devices"][key] = {"mac": mac, "client_id": str(uuid.uuid4())}
    info = ota(ident)
    if "activation" in info:
        linx.call("POST", f"/xiaozhi/device/bind/{agent_id}/{info['activation']['code']}")
        print(f"绑定设备 {key} {ident['mac']} -> {agent_id}")
    return ident


def provision(scenario_path: Path) -> dict:
    scenario = yaml.safe_load(scenario_path.read_text())
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {"agents": {}, "devices": {}}
    linx = Linx()
    try:
        for char in scenario["characters"]:
            key = f"{scenario['id']}.{char['id']}"
            agent_id = ensure_agent(linx, state, key, char["name"], char.get("role", ""),
                                    char.get("voice", ""), mcp=not char.get("player"))
            for part in (["ear"] if char.get("player") else ["brain", "mouth"]):
                ensure_device(linx, state, f"{key}.{part}", agent_id)
    finally:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="按剧本开通灵矽智能体与虚拟设备")
    parser.add_argument("scenario", type=Path, help="剧本文件，例如 scenarios/hotel_lobby/scenario.yaml")
    args = parser.parse_args()
    load_dotenv(find_dotenv(usecwd=True))
    state = provision(args.scenario)
    print(f"完成：{len(state['agents'])} 个智能体，{len(state['devices'])} 台设备，记录于 {STATE_PATH}")


if __name__ == "__main__":
    main()
