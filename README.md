# 灵矽语音游戏

七牛云第五届 1024 Hackathon · 研发赛道作品

> 议题：灵矽应用 —— 做一款语音类游戏（不能是跑团），如狼人杀。

基于[灵矽 AI](https://linx.qiniu.com/) 硬件智能体平台的语音游戏。玩法设计讨论中，确定后在此补充。

## 目录结构

```
.
├── docs/                 架构设计、产品设计文档
├── .env.example          环境变量模板
└── README.md
```

## 本地配置

```bash
cp .env.example .env
# 编辑 .env，填入灵矽控制台创建的 API Key
```

API Key 在[灵矽控制台 · API Key 管理](https://xrobo.qiniu.com/#/api-key-management)创建。`.env` 已被 `.gitignore` 忽略，不会提交。

## 开发流程

- 所有变更通过 Pull Request 合入 `main`，不直接推送 `main`。
- `v0` tag 标记初始化提交，之后的代码均为本项目新写。
- PR 描述中注明 AI 参与的部分（见 PR 模板）。

## 参考资料

- 灵矽 AI 官网：<https://linx.qiniu.com/>
- 灵矽控制台：<https://xrobo.qiniu.com/>
- 灵矽开发文档：<https://github.com/qiniu/Xrobot-docs>
