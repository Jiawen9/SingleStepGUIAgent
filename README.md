# SingleStepGUIAgent

SingleStepGUIAgent 是一个 Android 单步 GUI Agent：采集一次设备状态，选择一个动作，执行一次并退出。

默认执行流程：

```text
输入采集 → XML 指令前处理 → XML 引擎 → VLA 引擎 → 命令转换 → 设备执行 → 结果留档
```

XML 引擎未命中时才调用 VLA。截图和第一次 XML dump 在同一次任务中只采集一次，并由两个引擎共享。

VLA 截图通过 Qwen 官方 `qwen-vl-utils` 等比例处理，最多使用 1024 个视觉 token。模型输出的 `0～1000` 千分位坐标会直接映射回原始截图尺寸。

## 安装与配置

```powershell
python -m pip install -r requirements.txt
```

TTK 图形界面的设备画面预览需要 `scrcpy`。Windows 可以使用 WinGet 安装：

```powershell
winget install --exact Genymobile.scrcpy
```

安装完成后请重新打开终端或图形界面。

复制 `.env.example` 为 `.env`，然后填写本机配置：

```dotenv
MODEL_URL=https://example.com/v1
MODEL_NAME=your-model
MODEL_API_KEY=your-api-key
DEVICE_ID=your-device-serial
ADB_PATH=D:\platform-tools\adb.exe
```

也可以安装为标准 Python 工程：

```powershell
python -m pip install -e .
```

## 运行

设备需要提前出现在 `adb devices` 中，程序不会主动执行 `adb connect`。

```powershell
python -m orchestrator "IQY_001" "暂停视频"
```

常用参数：

```powershell
# 仅决策，不操作设备
python -m orchestrator "IQY_001" "暂停视频" --dry-run

# 指定设备
python -m orchestrator "IQY_001" "点击搜索" --serial HA223YB6

# 指定引擎顺序；--engine 可以重复
python -m orchestrator "IQY_001" "点击搜索" --engine xml --engine vla

# 查看完整参数
python -m orchestrator --help
```

## 动作协议

VLA 只返回一个扁平 JSON 动作，不使用 API function calling。

基础动作：

```json
{"action":"click","coordinate":[500,500]}
{"action":"type","text":"输入内容"}
{"action":"swipe","start_coordinate":[500,700],"direction":"up","distance":"medium"}
```

播放器和页面动作：

- `player_pause`
- `player_seek_to_start`
- `player_previous_episode`
- `player_next_episode`
- `player_set_quality`
- `player_set_playback_speed`
- `player_search`

拒绝动作：

```json
{"action_id":"reject","reason_type":"拒绝类型"}
```

`reason_type` 只允许：

- `TARGET_NOT_VISIBLE`：目标或执行动作所需的状态不可见。
- `UNSUPPORTED_TARGET`：用户提出的目标不受当前场景支持。

所有动作都会在本地完成参数白名单和范围校验后再转换为执行命令。

## 结果留档

每次任务写入 `screenshots/<case_id>/`：

- `<case_id>.png`：首次原始截图。
- `<case_id>_0.xml`：首次 UI XML。
- `<case_id>_done.png`：执行后的截图。
- `<case_id>_draw.png`：动作标注图。
- `prompt.txt`：实际发送给 VLA 的文本 Prompt。
- `result.json`：输入、各引擎结果、选中动作、执行命令、执行结果和耗时。
- `<case_id>_1.xml`、`<case_id>_2.xml` 等：原子工具执行期间产生的后续 XML。

## 工程结构

```text
SingleStepGUIAgent/
├─ orchestrator.py                         # Pipeline 总编排、参数解析和命令行入口
├─ contracts.py                            # 输入、动作、命令、执行结果等共享数据契约
├─ config.py                               # 环境变量读取和 AgentConfig
├─ gui_agent_ttk.py                        # TTK 图形界面和 scrcpy 预览
├─ input/
│  ├─ __init__.py                          # 输入包定义
│  └─ collector.py                         # 截图、前台包名和首次 XML 的一次性采集
├─ engines/
│  ├─ __init__.py                          # 决策引擎包定义
│  ├─ base.py                              # Engine 统一接口
│  ├─ registry.py                          # 按配置顺序装配引擎链
│  ├─ validation.py                        # 动作规格、规范化和参数校验
│  ├─ xml/
│  │  ├─ __init__.py                      # XML 引擎公开接口
│  │  ├─ engine.py                        # XML 确定性决策引擎
│  │  ├─ preprocessor.py                  # XML 指令前处理器及调度
│  │  ├─ instruction.py                   # 点击指令识别和目标词提取
│  │  ├─ matcher.py                       # 指令目标与 UI 节点匹配
│  │  └─ router.py                        # XML 点击规则路由和动作生成
│  └─ vla/
│     ├─ __init__.py                      # VLA 引擎公开接口
│     ├─ engine.py                        # VLA 决策引擎适配
│     ├─ client.py                        # 多模态 API 请求、Prompt 构造和响应解析
│     └─ prompts/
│        ├─ __init__.py                   # App Prompt 公开接口
│        ├─ registry.py                   # 根据前台包名选择 App Prompt
│        └─ iqiyi.py                      # 播放器组合动作 Prompt 定义
├─ output/
│  ├─ __init__.py                          # 输出转换包定义
│  ├─ commands.py                          # 标准动作转换为 ExecutionCommand
│  └─ serialization.py                     # 动作和流水线结果的 JSON 序列化
├─ execution/
│  ├─ __init__.py                          # 执行包定义
│  ├─ executor.py                          # 执行 ADB、原子工具或拒绝命令
│  ├─ timing.py                            # 原子工具结果与耗时协议
│  └─ atomic_tools/
│     ├─ __init__.py                      # 原子工具包定义
│     └─ iqiyi/
│        ├─ __init__.py                   # 播放器原子工具包定义
│        ├─ mode.py                       # 中屏定位与语义匹配模式配置
│        ├─ pause.py                      # 暂停播放
│        ├─ seek_to_start.py              # 定位到视频开头
│        ├─ change_episode.py             # 上一集和下一集
│        ├─ set_quality.py                # 切换清晰度
│        ├─ set_playback_speed.py         # 切换播放倍速
│        └─ search.py                     # 打开搜索页、输入并提交关键词
├─ device/
│  ├─ __init__.py                          # Android 设备访问包定义
│  ├─ adb.py                               # ADB 设备检查、截图和基础手势
│  └─ xml_hierarchy.py                     # UI XML dump、归档、解析和节点定位
├─ storage/
│  ├─ __init__.py                          # 留档包定义
│  └─ artifacts.py                         # 保存截图、Prompt、结果 JSON 和动作标注图
└─ tests/
   ├─ test_pipeline.py                     # Pipeline 契约和引擎回退测试
   ├─ test_single_step_agent.py            # 动作、API、执行器和原子工具测试
   ├─ test_app_prompts.py                  # 前台应用与 Prompt 加载测试
   ├─ test_uitree_rules.py                 # XML 指令和节点匹配规则测试
   ├─ capture_device_state.py              # 同时采集设备截图和 UI XML
   ├─ capture_phone_screen.py              # 真机截图调试工具
   └─ fast_dump_uiautomator2.py            # uiautomator2 快速 XML dump 工具
```

## 测试

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

真机辅助脚本位于 `tests/`，包括截图、XML dump 和设备状态采集工具。
