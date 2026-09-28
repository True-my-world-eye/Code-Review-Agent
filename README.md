# Code Review Agent

基于 LLM 的代码审查 Agent：输入一个文件或目录，Agent 自主完成「读取代码 → 调用工具分析 → 生成审查报告 →（可选）应用修复」的完整闭环，提供 **CLI** 与 **Web** 双界面，支持 DeepSeek / 小米 MiMo 等任意 OpenAI 兼容服务商一键切换。

> 设计文档见 [Design.md](Design.md) · 开发进度见 [docs/开发文档.md](docs/开发文档.md) · 测试记录见 [docs/测试文档.md](docs/测试文档.md)

## 功能特性

- 🔄 **ReAct Agent 循环**：推理 → 工具调用 → 结果回填，执行过程全程时间线可见
- 🛠 **5 个内置工具**：目录列举、分段读文件、正则搜索、ruff 真实 lint、安全自动修复
- ⚙️ **可视化服务商设置**：Web 设置面板 / CLI 命令切换服务商与 API Key，带连通性自检
- 💬 **上下文记忆**：会话内多轮追问，历史自动截断
- 🖥 **双界面**：rich 美化的 CLI + 简约风 Web UI（同一内核，行为一致）
- 🛡 **安全默认**：自动修复需逐次确认并自动备份、路径穿越防护、Key 脱敏且不入库

## 快速开始

```powershell
# 1. 安装依赖（Python ≥ 3.10）
uv pip install -r requirements.txt

# 2. 配置 LLM（二选一）
copy config.example.yaml config.yaml   # 编辑 config.yaml 填入 api_key
# 或使用环境变量：$env:CRA_API_KEY = "sk-..."

# 3. 运行
python main.py config test             # 测试连通性
python main.py review path/to/code.py  # 审查文件
python main.py chat                    # 交互式多轮对话
python main.py web                     # 启动 Web 界面（http://127.0.0.1:8000）
```

**Windows 用户也可以双击启动**（仓库根目录三个入口，自动选用项目虚拟环境）：

| 脚本 | 用法 |
|------|------|
| `启动Web界面.bat` | 双击 → 自动启动服务并打开浏览器 |
| `审查.bat` | 双击后输入路径，或**把文件/文件夹直接拖到图标上**即开始审查 |
| `打开聊天.bat` | 双击 → 进入交互式多轮对话 |

## 项目结构

```
├── main.py                # 统一入口（CLI 路由）
├── 启动Web界面.bat          # Windows 双击入口：Web 服务 + 自动开浏览器
├── 审查.bat                 # Windows 双击入口：拖拽文件即审查
├── 打开聊天.bat             # Windows 双击入口：交互式对话
├── app/
│   ├── config.py          # 配置中心（预设 / 优先级 / 脱敏）
│   ├── llm/               # LLM 适配层（重试 / 连通性自检）
│   ├── core/              # Agent 循环 / 记忆 / Prompt
│   ├── tools/             # 工具注册表与内置工具
│   ├── cli/               # CLI 界面
│   └── web/               # Web API（FastAPI）
├── index.html             # Web 前端入口（无构建，可直接预览）
├── css/  js/              # 前端样式与脚本
├── tests/                 # pytest 单元测试（120 个用例）
├── docs/                  # 开发文档 / 测试文档
└── Design.md              # 设计文档
```

## 快速演示

```powershell
# 用内置的「问题代码」体验完整审查链路（预埋 6 类典型缺陷）
python main.py review examples/app_demo.py

# 或启动 Web 界面（设置面板可切换 DeepSeek / 小米 MiMo 等服务商）
python main.py web
```

输出：实时工具时间线 + 分级审查报告（严重/警告/建议，含行号与修复建议）。

## 配置说明

配置优先级：**CLI 参数 > 环境变量（CRA_*）> config.yaml > 服务商预设**。

| 字段 | 说明 | 默认 |
|------|------|------|
| `provider` | `deepseek` / `mimo` / `openai-compatible` | `deepseek` |
| `base_url` | 端点，留空用预设 | 按预设 |
| `api_key` | 密钥，推荐用 `CRA_API_KEY` 环境变量 | 空 |
| `model` | 模型名，留空用预设（MiMo 预设 `mimo-v2.6-pro`） | 按预设 |
| `max_iterations` | Agent 循环上限 | 8 |
| `auto_fix_enabled` | 是否允许自动修复（仍需逐次确认） | true |

## 开发规范

- Git：main 只读，功能分支开发，自测通过后 `--no-ff` 合并
- 文档：代码变更同 commit 更新对应文档
- 注释：关键逻辑均带中文注释
- 测试：`.venv\Scripts\python.exe -m pytest tests/ -v`
