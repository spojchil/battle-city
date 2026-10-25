# 灵矽语音游戏：酒店大堂

七牛云第五届 1024 Hackathon · 研发赛道作品
视频 ：https://www.bilibili.com/video/BV11fnS6BEjA/?vd_source=90f2306d5da43021c3c15a4b60ac5f12
> 议题：灵矽应用 —— 做一款语音类游戏（不能是跑团），如狼人杀。

一款单人实时语音推理游戏。你在酒店大堂打了个盹，梦见大堂发生了爆炸。你从小做这种梦，而且从没错过。接下来的 20 分钟里，你在大堂平面图上自由走动，旁听 AI 角色之间的交谈，也可以直接开口和他们说话，用常开的录音笔和手机拍照收集证据，让警方在爆炸发生前介入。

- **世界按真实时间 1:1 推进**：没有主持人。你不行动，其他角色照样按自己的目标行事、彼此交谈。
- **声音有空间**：话语随距离和门衰减，离得远就听不清。线索要靠走近去听。
- **所有角色平等**：每个 AI 角色都是一个灵矽智能体，有自己的音色、背景和任务书，与玩家遵循同一套感知和行动规则。
- **结局由 AI 评判**：提交证据后，大模型以值班警官的视角审阅录音和照片，判断警方是否会提前介入。

架构与分工见 [docs/architecture.md](docs/architecture.md)。

## 目录结构

```
.
├── scenarios/hotel_lobby/   剧本：地图、角色、任务书、线索、评判标准
├── server/                  游戏服务（Python，uv 管理）
│   └── src/linx_game/
│       ├── engine/          通用引擎：世界状态、寻路、声音传播、感知
│       ├── runtime/         一局游戏：角色思考循环、发声、语音识别、行动工具、评判
│       ├── web/static/      网页客户端
│       ├── llm_gateway.py   大模型网关（灵矽自定义模型）
│       ├── app.py           服务入口
│       └── devtools/        开通脚本、虚拟设备等开发工具
└── docs/                    架构文档
```

## 运行

### 1. 准备

- **运行环境**：Python 3.12 与 [uv](https://docs.astral.sh/uv/)。
- **灵矽账号**：在[灵矽控制台](https://xrobo.qiniu.com/#/api-key-management)创建 API Key。
- **DeepSeek 官方 API Key**：大模型默认使用 `deepseek-flash`。也可以设置 `LLM_UPSTREAM=qiniu`，改用七牛 AI 大模型推理。
- **网络**：服务器需能从公网访问，灵矽平台会回调本服务的 MCP 与大模型网关。

```bash
cp .env.example .env   # 填入各项密钥；.env 已被忽略，不会提交
```

### 2. 在灵矽注册本服务

本服务在同一端口提供两个回调地址，需在灵矽各注册一次，并把返回的 ID 写入 `.env`：

| 用途 | 地址 | 注册方式 | `.env` |
|---|---|---|---|
| 行动工具（MCP） | `http://<服务器>/mcp` | 自定义 MCP，传输方式 Streamable HTTP | `LINX_MCP_ID` |
| 大模型网关 | `http://<服务器>/llm/v1` | 自定义模型（OpenAI 兼容）；api_key 填 `LLM_GATEWAY_TOKEN` | `LINX_LLM_MODEL_ID` |

### 3. 开通角色智能体与设备

```bash
cd server
uv run linx-provision ../scenarios/hotel_lobby/scenario.yaml
```

开通内容：

- 每个 AI 角色一个智能体，音色取自剧本，并开通两台虚拟设备。
- 玩家对应一个智能体和一台设备。

开通结果保存在 `~/.config/linx-game/provision.json`。脚本可以重复运行，只会补齐缺失的部分。

### 4. 启动

```bash
cd server
PORT=80 uv run linx-game
```

浏览器打开 `http://<服务器>/`，读完背景介绍后点击「开始」。浏览器只在 HTTPS 或 localhost 下允许使用麦克风；远程服务器可以先建立 SSH 隧道，再打开 `http://localhost:8080`：

```bash
ssh -L 8080:localhost:80 <服务器>
```

**操作**：

- WASD 或方向键移动，点击地图寻路前往。
- 靠近物品时按 E 交互，P 拍照。
- 按住空格说话，或在文本框中打字说话。
- 证据够了点「提交证据」。

**调试**：`GAME_OFFLINE=1` 时不连接灵矽，AI 角色不行动，可以单独检查界面。`GAME_DEBUG=1` 会记录平台消息。

## 测试

```bash
cd server
uv run pytest -q
```

## 开发流程

- 所有变更通过 Pull Request 合入 `main`，进度跟踪见 issue #4。
- `v0` tag 标记初始化提交，之后的代码均为本项目新写。
- PR 描述中注明 AI 参与的部分。

## 参考资料

- 灵矽 AI：<https://linx.qiniu.com/>，开发文档：<https://github.com/qiniu/Xrobot-docs>
- DeepSeek API：<https://api-docs.deepseek.com/>
