# SingleStepGUIAgent

SingleStepGUIAgent 是一个 Android 单步 GUI Agent：采集一次设备状态，选择一个动作，执行一次并退出。

默认执行流程：

```text
输入采集 → 点击指令前处理 → UITree/XML 引擎 → OCR 引擎 → VLA 引擎 → 命令转换 → 设备执行 → 结果留档
```

默认决策顺序为 `UITree/XML → OCR → VLA`。截图和第一次 XML dump 在同一次任务中只采集一次。OCR 与 UITree 共用点击指令解析；UITree 未命中后，OCR 会在原始截图的识别文本中精确匹配目标，达到置信度阈值时点击文本框中心，否则继续使用 VLA。

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

OCR 支持 PaddleOCR AI Studio 云端异步 API 和本地 PaddleOCR/PaddleX 服务。云端模式配置：

```dotenv
OCR_PROVIDER=cloud
OCR_CLOUD_JOB_URL=https://paddleocr.aistudio-app.com/api/v2/ocr/jobs
OCR_CLOUD_TOKEN=your-new-token
OCR_MIN_SCORE=0.5
OCR_DIAGNOSTIC_TOP_N=3
OCR_TIMEOUT_SECONDS=120
OCR_POLL_INTERVAL_SECONDS=5
OCR_CONNECTION_RETRIES=3
OCR_RETRY_BACKOFF_SECONDS=2
```

本地模式只需服务地址，服务端需预先加载 `PP-OCRv6_medium`：

```dotenv
OCR_PROVIDER=local
OCR_LOCAL_URL=http://127.0.0.1:8080/predict
OCR_MIN_SCORE=0.5
OCR_DIAGNOSTIC_TOP_N=3
OCR_TIMEOUT_SECONDS=120
```

云端 Token 只写入被 Git 忽略的 `.env`，不要提交到仓库。

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
python -m orchestrator "IQY_001" "点击搜索" --engine xml --engine ocr --engine vla

# 查看完整参数
python -m orchestrator --help
```

## Excel 批量评测

任务表的工作表名为 `测试用例集`，使用 `任务指令`、`图片ID`、`UI-TREE`、`二级能力`、`三级能力`、`结果输出` 六列。
评测只读取静态截图和 XML，不操作设备：

```powershell
# 默认：XML + OCR + VLA 串行回退，VLA 开启组合动作
python evaluator.py test.xlsx --workers 4

# 串行回退，VLA 仅开放基础动作
python evaluator.py test.xlsx --engine-strategy serial --vla-mode vla-basic --workers 4

# XML、OCR、VLA 并行全跑取并集，VLA 开启组合动作
python evaluator.py test.xlsx --engine-strategy parallel --vla-mode vla-combo --workers 4

# XML、OCR、VLA 并行全跑取并集，VLA 仅开放基础动作
python evaluator.py test.xlsx --engine-strategy parallel --vla-mode vla-basic --workers 4
```

评测固定启用 XML、OCR、VLA 三个引擎，不再通过命令行区分仅 VLA、XML+OCR 或 XML+OCR+VLA。`--engine-strategy` 与 `--vla-mode` 相互独立：前者控制三个引擎如何执行和汇总，后者只控制 VLA 的动作空间。

`--engine-strategy` 默认值为 `serial`：

- `serial`：按照 `XML → OCR → VLA` 串行回退；当前引擎只要选出动作即停止，只有不命中或发生可恢复错误时才进入下一个引擎。
- `parallel`：同时运行 XML、OCR、VLA，等待三个引擎全部完成后取正确结果并集；任意一个引擎判定正确，该用例即为综合正确。报告会保留每个引擎的动作、判分、错误和 artifact。

`--vla-mode` 默认值为 `vla-combo`：

- `vla-basic`：VLA 只开放基础动作。
- `vla-combo`：VLA 根据 UI-TREE 中识别到的前台 App 加载对应组合动作。

`--workers` 只控制不同 Excel 用例行之间的并发，与 `--engine-strategy`、`--vla-mode` 相互独立。使用 `parallel + workers=4` 时，最多可能同时运行 12 个引擎任务，请根据 OCR/VLA 服务限流能力调整。

总览按测试集能力列固定汇总为：`文本-清晰`（文本定位 + 意图清晰）、`文本-模糊`（文本定位 + 意图模糊）、`图标-清晰`（图标定位 + 意图清晰）、`图标-模糊`（图标定位 + 意图模糊）、`拒答`（二级能力为拒答）和`总体`。串行策略按最终选中动作统计；并行策略按各引擎正确结果的并集统计，并在能力分类表中并列给出 `XML+OCR` 并集和 VLA 的正确数、成功率。并行能力分类表中的成功率统一以对应能力分类的全部任务数为分母。

默认使用 `--engine-strategy serial --vla-mode vla-combo --workers 1`；通过 `--workers N` 可按用例行并发执行。VLA/OCR 服务可能有限流，建议从较小并发开始调整。
运行过程中会实时输出每条用例的 `START` 和 `PASS`/`FAIL`/`ERROR` 状态、完成进度与耗时；输出使用即时刷新，适合在远程终端观察。
单个模型响应无法解析或某个引擎抛出普通异常时，该错误只记录到当前引擎和用例，批量评测会继续执行其他引擎及后续用例；手动中断仍会正常停止程序。

评测结束后，可使用根目录的 TTK 人工检查工具打开结果报告：

```powershell
python evaluation_report_ttk.py test_result_20260819-120000.xlsx
```

工具左侧显示截图，并以红色叠加“期望结果”、蓝色叠加“系统像素动作”；右侧列出任务、`VLA正确` 和判分说明。报告应与 `device_captures` 目录位于同一级目录。

结果默认保存为 `test_result_<时间戳>.xlsx`，包含原任务表、评测明细和总览。

参考答案可使用 TTK 标注工具逐行编辑，翻页和关闭时自动保存：

```powershell
python annotation_tool_ttk.py test.xlsx
```

标注工具从工程根目录的 `device_captures` 读取图片和 UI-TREE；例如图片ID
`IQY_001` 对应 `device_captures/IQY_001.png`，UI-TREE 可填写
`IQY_001_0.xml`。绝对路径仍可直接使用。

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
{"action":"reject"}
```

网易云音乐包名 `com.netease.cloudmusic.iot` 会额外加载搜索动作协议：

```json
{"action":"player_search","query":"搜索词"}
```

当前仅提供 Prompt、动作目录和参数校验，尚未实现对应组合工具，不能在设备执行阶段运行该动作。

喜马拉雅包名 `com.ximalayaos.pad` 会加载以下动作协议：

```json
{"action":"player_search","query":"搜索词"}
{"action":"player_set_playback_speed","speed":"1.5x"}
{"action":"player_set_sleep_timer","minutes":30}
```

允许的倍速为 `0.5x`、`1.0x`、`1.5x`、`2.0x`、`2.5x`、`3.0x`；定时暂停分钟数为 `15`、`30`、`60`、`90`。当前同样只提供 Prompt、动作目录和参数校验，尚未实现组合工具。

抖音包名 `com.ss.android.ugc.aweme` 会加载以下动作协议：

```json
{"action":"player_search","query":"搜索词"}
{"action":"player_set_playback_speed","speed":"1.25x"}
{"action":"player_pause"}
{"action":"player_next_episode"}
```

允许的倍速为 `0.5x`、`1.0x`（正常）、`1.25x`、`1.5x`。当前只提供 Prompt、动作目录和参数校验，尚未实现组合工具。

腾讯视频包名 `com.tencent.qqlive.audiobox` 会加载以下动作协议：

```json
{"action":"player_search","query":"搜索词"}
{"action":"player_set_playback_speed","speed":"1.25x"}
{"action":"player_pause"}
{"action":"player_resume"}
{"action":"player_previous_episode"}
{"action":"player_next_episode"}
```

允许的倍速为 `0.5x`、`0.75x`、`1.0x`、`1.25x`、`1.5x`。当前只提供 Prompt、动作目录和参数校验，尚未实现组合工具。

以下两种情况都使用无参数的 `{"action":"reject"}`：目标或执行所需状态在当前截图中不可见、无法可靠确认；或者动作空间中没有一个动作能独立完成用户的完整意图。拒绝输出不携带原因分类。

所有动作都会在本地完成参数白名单和范围校验后再转换为执行命令。

## 结果留档

每次任务写入 `screenshots/<case_id>/`：

- `<case_id>.png`：首次原始截图。
- `<case_id>_0.xml`：首次 UI XML。
- `<case_id>_done.png`：执行后的截图。
- `<case_id>_draw.png`：动作标注图。
- `<case_id>_ocr.png`：OCR 全部识别文本框、文字和置信度标注图。
- `prompt.txt`：实际发送给 VLA 的文本 Prompt。
- `result.json`：输入、各引擎结果、选中动作、执行命令、执行结果和耗时。
- `<case_id>_1.xml`、`<case_id>_2.xml` 等：原子工具执行期间产生的后续 XML。

## 工程结构

```text
SingleStepGUIAgent/
├─ orchestrator.py                         # Pipeline 总编排、参数解析和命令行入口
├─ evaluator.py                            # Excel 静态任务批量评测、判分和汇总
├─ evaluation_report_ttk.py                # 评测报告红蓝动作叠加人工检查工具
├─ annotation_tool_ttk.py                  # Excel 用例参考答案可视化标注工具
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
│  ├─ instruction.py                       # UITree 与 OCR 共用的点击指令解析
│  ├─ preprocessing.py                     # 共用前处理器及调度
│  ├─ xml/
│  │  ├─ __init__.py                      # XML 引擎公开接口
│  │  ├─ engine.py                        # XML 确定性决策引擎
│  │  ├─ matcher.py                       # 指令目标与 UI 节点匹配
│  │  └─ router.py                        # XML 点击规则路由和动作生成
│  ├─ ocr/
│  │  ├─ __init__.py                      # OCR 引擎公开接口
│  │  ├─ client.py                        # 云端异步与本地同步 PaddleOCR 客户端
│  │  └─ engine.py                        # OCR 文本匹配、阈值与点击动作生成
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
