# 03 · Python 守护进程架构与 Node↔Python 桥

> 调研对象：`dsh-vhcu`（插件 1.1.0，仓库 HEAD `f118ba5`，分支 `main`）。
> 调研方式：**只读静态阅读**。未启动守护进程、未执行 Python/Node 测试、未修改源码、未安装或升级宿主。
> 宿主保持 0.2.1-rc3；本文最后一部分列出对 0.2.2-rc5 的升级敏感点。
> 约定：`文件:行` 均指本仓库相对路径；标注「事实」= 代码可直接读出，「推断」= 基于代码的合理判断。

---

## 1. 进程模型与协议

### 1.1 启动链

```
Node supervisor.spawn(python, ['-u','-m','vhcu_daemon','--parent-pid',<hostPid>,'--protocol',1])
  └─ vision/vhcu_daemon/__main__.py:3-6 或 entrypoint.py:11-19
       └─ cli.main()                       cli.py:1007-1030
            ├─ build_parser()              cli.py:994-1004
            ├─ Daemon(args)                cli.py:115-171
            │    ├─ Protocol(...)          protocol.py:62-80  ← 关键：劫持 fd 1
            │    ├─ DriverFactory.create() drivers/factory.py:14-29
            │    ├─ Runner(...)            runner.py:36-82
            │    ├─ HttpFrameServer(...)   httpframe.py:63-86
            │    ├─ UiaWorker/ElementRegistry/ElementActionService  cli.py:161-165
            │    └─ HearService（经 WindowsAudioBackend 构造） drivers/windows.py:41-46
            └─ Daemon.run()                cli.py:175-227
                 ├─ pidfile.reclaim_orphans()   cli.py:177-181 / pidfile.py:221-275
                 ├─ write_record(parentPid)     cli.py:183 / pidfile.py:141-157
                 ├─ HttpFrameServer.start()     cli.py:186 → 先监听再 READY
                 ├─ input.release_all()         cli.py:189-192（清掉崩溃残留的按键）
                 ├─ proto.ready(...)            cli.py:193-211
                 ├─ heartbeat 线程              cli.py:212,231-262
                 ├─ parent-pid watchdog         cli.py:213-214,305-320
                 └─ _command_loop()             cli.py:324-356
```

- READY 只在图像平面已监听后发送（`cli.py:186` 在 `cli.py:193` 之前），READY 携带 `port`、`token`、`capabilities.sources`、`input.status()`、`hear.status()` 与 Python 版本（`cli.py:193-211`）。
- `Protocol` 构造时 `os.dup(1)` 保存真实 stdout，再把 fd 1 指向 stderr，并把 `sys.stdout` 换成 stderr（`protocol.py:74-80`）。任何库的 `print`/traceback 只落 stderr，不会污染帧流。这是 Bridge 稳定性的第一道保险。
- stdin EOF 视为父进程消失，直接退出（`cli.py:327-329`）；Windows 上父进程崩溃不会自动杀子进程，所以另有 watchdog：每 2s 检查 `--parent-pid`，父进程消失即 `os._exit(0)`（`cli.py:305-320`）。
- 绝对路径契约：`VHCU_MODEL_PATH` / `VHCU_MACROS_PATH` / `VHCU_LOG_DIR` 必须是绝对路径，相对路径抛 `PathConfigError`（`paths.py:31-44`）。注意 `HearService.__init__` 会立即调用 `default_model_dir()`（`hear.py:554` → `paths.py:52-53`），因此**非法的 VHCU_* 环境变量会让守护进程在 READY 之前崩溃**，宿主侧表现为 READY 超时（推断，未实跑验证）。

### 1.2 NDJSON 帧格式

双向都是「一行一个 JSON 对象，UTF-8，LF，≤64 KiB」：

- Python 侧上限 `MAX_LINE_BYTES = 65536`（`protocol.py:18`），发送时超限抛 `E_PROTO`（`protocol.py:100-101`），接收时超限丢弃并回 `error` 帧（`cli.py:330-333`）。
- 入站校验要求 `v/seq/type/ts` 四个信封字段，`v` 必须等于 1，`seq` 为正整数，`type ∈ {cmd, shutdown}`；`cmd` 还要求非空 `id`、`op`，`args` 必须是对象（`protocol.py:140-166`）。
- 出站帧统一带 `v`、`seq`、`type`、`ts`，再合并业务字段（`protocol.py:91-97`）。
- Node 侧用 JSON Schema 二次校验：信封 + 按 `type` 选 schema（`contract/lib/contract.js:191-234`），解析失败不抛异常而是返回 `{ok:false, code}`（`contract/lib/contract.js:278-296`）。
- 关键帧类型：`ready`、`ack`、`event`、`heartbeat`、`error`、`snapshot`、`cmd`、`shutdown`（`contract/lib/contract.js:191-200`；Python 侧对应 `protocol.py:106-129`）。

### 1.3 「单调序号 + 绝对 deadline」的实现位置

**单调序号（事实）**

- Python 发送侧：`Protocol._seq` 在每次 `send()` 时 `+= 1`，整包加锁，保证单进程内单调（`protocol.py:86-104`）。daemon 重启后从 1 重新开始，这是合法的。
- Node 接收侧：每个子进程新建一个 `SeqTracker`（`host/lib/supervisor.js:366-368`），`accept(seq)` 区分 `next` / `gap` / `duplicate`（`contract/lib/contract.js:302-331`）。
- gap 不是丢弃，而是「报 `E_SEQ` 但仍接收帧」（`host/lib/supervisor.js:436-446`；契约注释 `contract/lib/contract.js:5-7`）；duplicate 直接丢弃并报 `E_SEQ`。
- host 自己的 `hostSeq` 从 1 起单调递增，用于 cmd/shutdown（`host/lib/supervisor.js:91,159,654`）。Python 侧**不校验**入站 seq 的连续性（`parse_line` 只校验正整数），只把它当协议信封字段。

**绝对 deadline（事实）**

- Node 发送命令时写入 `deadlineMs = Date.now() + timeoutMs`（`host/lib/supervisor.js:155-164`；
- Python 在执行前调用 `deadline_expired(frame)`，用本机 `time.time()*1000` 与 `deadlineMs` 比较，超时即抛 `E_TIMEOUT`「command deadline expired before execution」，**不执行任何动作**（`cli.py:74-90, 358-364`）。这就是「幽灵动作」防护：命令在管道里排队过久，宁可失败也不迟到执行。
- 两侧时钟必须同机同源（loopback 子进程），跨机部署会破坏该假设（推断）。

### 1.4 心跳、退出与停机

- 心跳线程每 1s 发一帧 `heartbeat`，包含 uptime、frames/diffs/dropped/fps/loopLagMs/rssBytes/vision 状态（`cli.py:231-262`）。心跳在视觉停止时仍继续（`cli.py:5`）。
- Node 看门狗：`heartbeatMs(1000) * heartbeatMissLimit(10)` 内无心跳即判定死亡并走 `_fail('heartbeat-lost')`（`host/lib/supervisor.js:529-545`；常量 `contract/lib/contract.js:23-24`）。
- 停机三级：`shutdown` 帧 → `SIGTERM` → 平台级 kill tree（`host/lib/supervisor.js:637-681,709-732`；Windows 用 `taskkill /T /F`）。
- Python 收到 `type:"shutdown"` 时**不回 ack**（无 cmd id，回了就是非法 ack），直接返回 0（`cli.py:342-345`）；若以 `op:"shutdown"` 的 cmd 形式到达，则回 ack 后退出（`cli.py:763-765`）。
- SIGINT/SIGTERM 触发 `interrupt.request + shutdown + os._exit(0)`（`cli.py:1016-1028`）。
- 正常退出路径 `shutdown()` 依次：停心跳、`release_all()`、停 Hear、停视觉、清元素表、关 UIA、关 overlay、删 PID 记录、广播 `daemon.bye`、停 HTTP、关协议（`cli.py:950-991`）。

---

## 2. 命令面

命令白名单是契约里的 `OPS`，共 **27** 项（`contract/lib/contract.js:35-63`）。Node 在 `send()` 入口先拒绝白名单外的 op（`host/lib/supervisor.js:146-149`），Python 在 `_dispatch()` 末尾对未知 op 抛 `E_DENIED`（`cli.py:766`）。两边是双保险，但**白名单只有 Node 侧一份**，Python 没有独立清单（事实）。

| op | Python handler | 关键参数 | 主要返回 / 错误码 |
|---|---|---|---|
| `ping` | `cli.py:369-371` | — | `{pong,pid}` |
| `sys.info` | `cli.py:372-391` | — | pid/python/platform/protocol/daemon/sources/cwd/input/paths/interrupt/uia |
| `sys.active_window` | `cli.py:392-406` | — | `{title,active_window,hwnd,pid,ts}` |
| `uia.find` | `cli.py:407-443` | `hwnd?`,`locator`,`options`,`timeoutMs`,`allowUnprotected` | 异步线程回 ack；`E_UNAVAILABLE`/`E_PRIVACY_BLOCKED`/`E_UIA_*`/`E_PROTO` |
| `uia.click` | `cli.py:444-475` | `elementId`,`retryMaxAttempts`,`allowUnprotected` | 异步线程；`E_ELEMENT_NOT_FOUND`/`E_ELEMENT_STALE`/`E_CAPTURE`/`E_UIA_*` |
| `uia.click_point` | `cli.py:476-527` | `x`,`y`,`clickType`,`allowUnprotected`,`humanizeProfile` | Invoke 或降级坐标点击 |
| `vision.start` | `cli.py:528-535` | `source`,`fps`,`privacyBlacklist` | `{source,fps}`；`E_STATE`/`E_PROTO`/`E_UNAVAILABLE` |
| `vision.configure` | `cli.py:536-539` | `privacyBlacklist` | 当前黑名单与 blocked 状态 |
| `vision.stop` | `cli.py:540-547` | `reason?` | `{stopped}`；停止前先关 overlay |
| `vision.status` | `cli.py:548-550` | — | Runner.status() |
| `vision.snapshot` | `cli.py:551-554` | — | ack + `snapshot` 帧（`asOfSeq`） |
| `vision.capture` | `cli.py:555-572` | `targetHwnd?`,`allowUnprotected`,`strictOcclusion`,`privacyBlockMode` | 目标窗口或全屏一帧 |
| `input.click` | `cli.py:573-595` | `x,y`,`scaleFactor/scaleX/scaleY`,`humanizeProfile`,`cursorInjectionMode`,`targetHwnd`,`clickType` | 见 §3.3；`E_PROTO`/`E_BUSY`/`E_DENIED`/`E_PRIVACY_BLOCKED`/`E_VIRTUAL_CURSOR_*` |
| `input.type` | `cli.py:596-608` | `text`,`imeShortcut`,`useClipboardFallback` | `E_PROTO`/`E_CLIPBOARD`/`E_MACRO_FAILED` |
| `input.hotkey` | `cli.py:609-620` | `keys[]`,`delayMs` | |
| `input.scroll` | `cli.py:621-638` | `amount`,`cursorInjectionMode`,`targetHwnd` | |
| `input.drag` | `cli.py:664-685` | `x1,y1,x2,y2`,`humanizeProfile`,`cursorInjectionMode`,`targetHwnd` | |
| `input.draw_shape` | `cli.py:639-663` | `shape,start/end`,`curve`,`samples`,`durationMs` | `E_PROTO` |
| `input.middle_drag` | `cli.py:686-707` | `x1,y1,x2,y2` | |
| `input.macro.open_notepad` | `cli.py:708-718` | `text`,`imeShortcut` | Win+R → notepad → 输入 |
| `input.macro.save` | `cli.py:719-727` | — | Ctrl+S |
| `input.macro.run` | `cli.py:728-747` | `steps[]`,`variables`,`scaleFactor/scaleX/scaleY`,`originX/Y` | `E_MACRO_FAILED`/`E_VIRTUAL_CURSOR_UNSUPPORTED` |
| `hear.start` | `cli.py:748-751` | — | `E_UNAVAILABLE`/`E_STATE`/`E_AUDIO_DEVICE` |
| `hear.stop` | `cli.py:752-755` | — | 8s join 超时 → `E_STATE` 且不可再 start（`hear.py:646-672`） |
| `hear.status` | `cli.py:756-758` | — | HearService.status() |
| `hear.configure` | `cli.py:759-762` | `level`,`disableFilter` | `E_PROTO` |
| `shutdown` | `cli.py:763-765` | — | `{bye:true}` 后返回 False 结束循环 |

**任务书里的 `record.*` / `macro.*` 需要更正（事实）**：
- 不存在 daemon 级 `record.*` 命令。全局键鼠录制在 Node 侧，用可选依赖 `uiohook-napi`（`lib/index.js:60,329-333,3612-3623`，工具 `vhcu_start_recording`/`vhcu_stop_recording`，`lib/index.js:4895-4915`）。
- 宏存储/队列/审批/断点续跑也在 Node 侧（`host/lib/macros.js`, `host/lib/session-state.js`, `host/lib/guards.js`）。Python 只负责「给定 steps 执行」（`input.run_macro`）和两个内置动作宏。

**错误码的真实约束（重要事实）**：
- 契约枚举有 37 个码（`contract/lib/contract.js`；ack/error schema 同步，`contract/schema/v1/ack.json`）。
- Python `protocol.ERROR_CODES` 有 **34 个**（2026-11-28 修正后）：契约 37 − Node 专用 3
  （`E_MACRO_GENERATION_DISABLED`、`E_MACRO_NOT_REUSABLE`、`E_SCRIPT_WRITE_DENIED`）。
  修正前只有 29 个，缺 5 个 daemon 实际会抛的码，导致经 `ProtocolError` 时被降级成 `E_INTERNAL`：
  `E_CLIPBOARD`、`E_AUDIO_DEVICE`、`E_FOCUS_LOST`、`E_MACRO_FAILED`、`E_MEMORY`（**已补齐**）。
- `E_CONFIG` 是**本地/启动期专用码**（`paths.PathConfigError`，相对路径的 VHCU_* 环境变量），
  在 READY 之前就抛出，宿主看到的是 READY 超时，不会出现在协议帧里；因此它**不在**契约枚举中，
  Python 侧单列为 `LOCAL_ONLY_CODES`。
- `ProtocolError.__init__` 对不在白名单里的 code 仍做 `E_INTERNAL` 兜底（这是契约保护，保留）。
  现在新增了两道护栏（`vision/tests/test_error_codes.py`）：
  ① daemon 包里出现的每个 `E_*` 字面量必须在 `ERROR_CODES ∪ LOCAL_ONLY_CODES` 中；
  ② Python 与 `contract/lib/contract.js` 的枚举做跨语言一致性校验（差集必须正好是那 3 个 Node 专用码）。
- 结论：模型/宿主看到的错误码现在与契约声明一致；今后新增错误码必须同时改契约与白名单，否则测试会挂。

---

## 3. 能力实现边界

### 3.1 捕获（`win_capture.py` + `sources.py`）

- 三种 source（`sources.py:185-195`）：`fake`（确定性动画，CI 用，`sources.py:45-80`）、`mss`（虚拟桌面/指定 monitor，`sources.py:83-139`）、`window`（单 HWND，`sources.py:143-182`）。
- `mss` 前强制 DPI 感知，避免 1536 逻辑宽当 1920 物理宽（`sources.py:89-96`，`win_capture.py:183-205`：优先 `SetProcessDpiAwarenessContext(-4)`，回退 `SetProcessDpiAwareness(2)`，再回退 `SetProcessDPIAware`）。
- 目标窗口捕获顺序：`PrintWindow(PW_RENDERFULLCONTENT)` → `PrintWindow(0)` → `WM_PRINT` → 屏幕 `BitBlt`（`win_capture.py:7-17,398-422`）。每个候选做稀疏质量检测，近黑（>99.5%）或近单色（dominant > 99.9%）判 blank 并换后端（`win_capture.py:225-259`）。
- BitBlt 兜底有严格前置：`strict_occlusion && occluded` → `E_CAPTURE_OCCLUDED`；被遮挡 → 同样拒绝；非前台可见 → 拒绝（`win_capture.py:424-444`）。注释明确「否则会拍到别的窗口像素」。
- 最小化 → `E_CAPTURE_MINIMIZED`（`win_capture.py:390-395`）；HWND 无效/已销毁 → `E_CAPTURE_TARGET_NOT_FOUND`（`win_capture.py:387-388`）；尺寸 > 8192 → `E_CAPTURE_UNSUPPORTED`（`win_capture.py:29,217-221`）。
- 隐私：`Runner.capture_now`/`capture_window` 在敏感标题下**不抓像素**，清空全部帧缓存后合成占位图（warning/black 两种，`sources.py:198-233`），返回 `privacyBlocked/privacyImage` 标志（`runner.py:381-409,448-487`）。后台 diff 循环在隐私阻断时根本不 `grab()`（`runner.py:534-541`）。
- 帧缓存：全屏 on-demand 帧按 `frame_seq` 保留 4 张（`runner.py:26,292-312`）；目标窗口帧额外保留 8 张（`runner.py:307-312`）。`frame_by_seq` 找不到就返回 None，HTTP 端不回退到 latest（`httpframe.py:152-161`），防止「张冠李戴」。

### 3.2 差分（`diff.py`）

- 纯 Python 粗网格：每格采中心 1 像素，三通道任一差值 > `threshold`（默认 14）即标记（`diff.py:63-83`）；网格步长由 `default_step(width,height,max_cells=100)` 决定（`sources.py:248-250`）。
- 连通块合并成 ROI，`min_cells=2` 起报，最多 8 个；按 `score*面积` 排序（`diff.py:86-134`）。
- 每个 ROI 带 8 字节 blake2s 采样哈希（`diff.py:144-160`）。
- 首帧/尺寸变化不产生 ROI（`diff.py:59-60`）。事件帧为 `vision.diff`，含 screen/rois/foreground/stats（`runner.py:589-604`）。
- 注意：差分只服务「事件时间轴」，动作层的精确 ROI 由 Node 侧裁剪/渲染，不在 Python 精修（`diff.py:1-7` 注释）。

### 3.3 输入（`input.py`）

- 注入只用 `SendInput`（`input.py:1770-1778`），失败非零即 `E_INTERNAL`；键盘 Win 键强制加 `EXTENDEDKEY` 并用扫描码（`input.py:1753-1765`，注释指向 whitepaper 8.9）。
- 物理安全闸：
  - 越界坐标夹到虚拟桌面范围并带 `warning:"坐标越界，已修正"`（`input.py:313-340,746-747`）；
  - 四角急停：光标在距角 8px 内 → `E_DENIED`「mouse failsafe」（`input.py:87,1719-1728`，纯函数在 `safety.py:32-45`）；
  - 频率限制 12 次 / 5s → `E_BUSY`（`input.py:90-91,1733-1739`）；
  - 前台断言：调用方给 `expectedForeground.hwnd` 且实际前台变了 → `E_FOCUS_LOST`（`input.py:493-508`）——但该码会被降级为 `E_INTERNAL`（见 §2）；
  - 隐私：`assert_privacy()` 在任何注入前 fail-closed，检查器异常也视为阻断（`input.py:415-441`；注入实现 `cli.py:805-828`）。
- 启动/退出无条件 `release_all()`：对 `RELEASE_VKS` 与鼠标三键发送 KEYUP/UP，防止崩溃后按键卡住（`input.py:98-110,457-491`；调用点 `cli.py:189-192,955-959`）。
- 注入模式三选一 `physical|virtual|hybrid`（`input.py:518-527`）：
  - `virtual` 走 PostMessage，不移动物理光标（`input.py:688-701`；实现 `virtual_cursor.py:347-446`）；
  - `hybrid` 在「首条消息尚未投递」时才允许降级为物理，投递过就不重放，避免双击副作用（`virtual_cursor.py:7-11`；`input.py:777-797`）。
- 拟人化：`off|precise|balanced`（`human_input.py:21-29`），balanced 为三次贝塞尔 + ease-in-out + 低频抖动，按绝对 deadline 逐点发送（`input.py:567-615`）；虚拟模式用 `virtual_duration_ms`（80-100ms）单独计时（`input.py:648-676`）。
- 高密度绘制：`draw_shape` 支持 line/parabola/sine/cosine，采样 160（上限 512），`curve` 为垂直幅度，最终由 `shapes.py:34-125` 生成并夹屏。
- 宏执行：1..100 步、≤20 个变量，`{{name}}` 只在 text/x/y/x1/y1/x2/y2 上替换（`input.py:1435-1460,1523-1553`）；坐标先乘 scale 再加 origin 并夹屏；`virtual` 模式拒绝宏回放（`input.py:1445-1450`）；步骤失败统一 `E_MACRO_FAILED`（`input.py:1484-1498`，同样会被降级）。

### 3.4 UIA（`uia_worker.py` + `element_registry.py`）

- 单 COM 线程 + 有界队列（`maxsize=1`），所有请求串行化，COM 对象不出线程（`uia_worker.py:1-12,665-765`）。`uia.find` 在 CLI 里再起一个请求线程，避免阻塞命令循环（`cli.py:424-443`）。
- 超时策略：请求超过 `timeout_ms`（默认 2500，调用点给 2500/4000）即标记 `_degraded` **且** `_unhealthy`，后续请求直接 `E_UIA_UNAVAILABLE`，提示「重启 daemon 才能恢复」——因为无法取消一个卡住的 COM 调用（`uia_worker.py:676-679,789-798,902-913`）。队列满 → `E_UIA_TIMEOUT`（`uia_worker.py:785-786`）。
- 发现语义：BFS，`max_depth` 1..10（默认 6）、`limit` 1..200（默认 50）、`node_budget` 1..5000（默认 500），超限置 `truncated`（`uia_worker.py:25-34,177-183,341-389`）。
- 坐标：优先信任落在窗口矩形内的原始 rect（`screen_physical`）；否则按 `dpi/96` 放大再验一次；都不在则标 `unknown` 并 `bounds_valid=false`（`uia_worker.py:223-252,604-662`）。
- 密码：`IsPassword` 为真时**连 ValuePattern 探针都跳过**（`uia_worker.py:596,628-631`），`value` 恒为 `null`、`value_redacted=true`；只有显式 `allow_value_read=true` 且非密码才读 Value，截断 2000 字符（`uia_worker.py:606-616`）。默认 `allow_value_read=false`（`uia_worker.py:182`）。
- 元素令牌：`el_` + `secrets.token_urlsafe(12)`；记录 hwnd/locator/options/bounds/64 位 dHash/window_rect；TTL 300s、最多 512 条，LRU 淘汰（`element_registry.py:13-17,113-215`）。指纹计算失败不影响发令牌（`element_registry.py:150-176`），截图失败也不影响注册（`element_registry.py:139-144`）。
- stale 判定：重查元素 → 重新截图 → 重算 dHash；`bounds` 四边任一差 ≤2 且 Hamming 距离 ≤10 才算新；否则重试（指数退避 0.05·2^n，上限 0.4s），耗尽抛 `E_ELEMENT_STALE`（`element_registry.py:265-378`）。错误分类严格：令牌缺失 `E_ELEMENT_NOT_FOUND`、截图失败 `E_CAPTURE`、UIA 内部异常 `E_UIA_FAILED`，只有真正的 bounds/hash 变化才是 stale（`element_registry.py:265-272,314-343`）。
- 点击：`prepare` 通过后调 `worker.invoke`；InvokePattern 可用则 Invoke，否则返回坐标计划由输入层物理点击（`uia_worker.py:408-504`；`element_registry.py:380-395`）。

### 3.5 虚拟光标（`cursor_overlay.py` + `virtual_cursor.py`）

- overlay：96 DIP 的小型分层窗口，`WS_EX_LAYERED|TRANSPARENT|TOOLWINDOW|TOPMOST|NOACTIVATE`，`UpdateLayeredWindow` 32bpp ARGB，`WM_NCHITTEST` 返回 `HTTRANSPARENT` 保证点击穿透（`cursor_overlay.py:1-18,32-48,1012-1032`）。
- 绘制：GDI+ 矢量圆角三角（默认、版权安全），可选 `assets/cursor.png` 着色；`move` 青色、`click` 红色 + 0.18s ripple、`drag` 绿色（`cursor_overlay.py:72-82,839-882,926-955`）。
- 「白线修复」：`OUTLINE_DIP=1.5` 纯白描边，只在实心光标上画，保证同色背景下可见（`cursor_overlay.py:56-58,880`；测试 `vision/tests/test_cursor_overlay.py::test_white_rim_is_limited_to_solid_cursor`）。
- 截图防污染：模型侧截图前 `hide_sync()` 隐藏 overlay，截图后 `restore_after_capture()` 恢复；后台 diff 不隐藏，避免 8-15fps 闪光标（`runner.py:255-279`；`cursor_overlay.py:368-397`）。overlay 是纯装饰，任何异常只报 `cursor.overlay_error`，不打断命令（`cli.py:150-159`；`cursor_overlay.py:16-17`）。
- PostMessage 注入：先 `SendMessageTimeoutW(WM_NULL, 50ms)` 预检消息队列，再 `ScreenToClient` + 客户区检查 + DPI 映射；只接受客户区内点击；`PostMessage` 失败且已按下时补发 UP；已投递过就抛带 `sent` 的失败，交给 hybrid 决定是否降级（`virtual_cursor.py:347-446`）。
- 明确拒绝 shell/系统窗口类（任务栏、Progman、开始菜单等）与 CEF/UWP 容器类（`virtual_cursor.py:51-75,299-321`）；Chromium/Electron 主窗口在 V1.1 被放行（注释 `virtual_cursor.py:68-71`）。

### 3.6 Hear（`hear.py`）

- 仅系统内录（WASAPI loopback，`soundcard` 默认扬声器），不用麦克风、不做 TTS/对话（`hear.py:1-10,485-540`）。
- 参数：16 kHz / 单声道 / 30ms 帧；VAD 静音切分 0.6s；最短语音 0.3s；最长段 8s 强制切；重叠 0.5s；首尾各补 0.2s 静音（`hear.py:24-34,289-318,745-800`）。
- 模型固定 `base`、CPU int8（`hear.py:78,565-584`）；优先本地离线目录 `<root>/faster-whisper-base/model.bin` 或 `<root>/model.bin`，带 `local_files_only=True` 且不传 `download_root`，确保不发网络请求（`hear.py:170-260`）；缺失才回退 HF 镜像下载，`HF_ENDPOINT` 在 import 期就强制为 `https://hf-mirror.com` 并补丁已导入的常量（`hear.py:86-103,251-260`）。
- 伪流式：VAD 触发 + 8s 强制切 + 0.5s 重叠 + `dedupe_overlap` 去重前缀（`hear.py:309-318,774-798,904-910`）。
- 幻听过滤：短语白名单、绝对黑名单（"thank you for watching" 等）、`no_speech_prob` 阈值、短段低 RMS、能量尖峰等组合决策（`hear.py:39-77,360-484,880-902`）；`configure(level)` 提供 strict/normal/sensitive 三档，热更新（`hear.py:51-77,592-606`）。
- 事件：`hear.text`（text/ts/durationMs/audioMs/language/languageProbability/model）与 `hear.audio`（纯音乐/环境音，含 rms/rms_max/rms_min/has_spike，`hear.py:831-844,916-925`）。
- 设备断开自动重连（1s 后重开 recorder），状态机含 `starting/loading/listening/reconnecting/error/idle`（`hear.py:608-672,714-820`）。`stop()` 8s join 超时则置 `unhealthy`，拒绝再次 start（`hear.py:646-672`）。
- 音频设备枚举/打开都有超时包装，死锁抛 `E_AUDIO_DEVICE`（`hear.py:120-156`）——同样会被降级为 `E_INTERNAL`。

### 3.7 帧图床（两层认证，事实）

```
模型 markdown 图片
  → http://<dsh web origin>/vhcu/frame?x&y&w&h&maxw&exp&seq&sig   （Node 路由）
  → http://127.0.0.1:<daemon port>/frame?...&token=<daemon token> （Python 图床）
```

- 第一层（Node）：每个插件实例生成 32 字节 `frameSecret`，URL 含 `exp = now + 5min`，`sig = HMAC-SHA256(x|y|w|h|exp[|seq])`；校验用 `timingSafeEqual`，过期或签名不符返回 403（`lib/index.js:527-572,1706-1721`）。这是真正的「短期签名 URL」。
- 第二层（Python）：`HttpFrameServer` 绑 `127.0.0.1`、端口 0（系统分配），token 为 daemon 启动时 `os.urandom(24).hex()`（`cli.py:134-142`；`httpframe.py:63-103`）。`/frame`、`/health`、`/interrupt` 都要求 `?token=`，用 `hmac.compare_digest` 比较（`httpframe.py:116-150`）。
- 并发上限 2（`MAX_INFLIGHT=2`），超出 429（`httpframe.py:21,137-145`）；响应头带 `x-frame-size/origin/backend/blank/occluded/seq`（`httpframe.py:173-185`），Node 用 HEAD 取元数据（`host/lib/frames.js:97-147`）。
- token 本身**没有过期时间**，只在 daemon 生命周期内有效；5 分钟 TTL 完全由 Node 层 `exp` 承担（事实）。README 的 "short-lived signed URLs"（`README.md:88`）因此只对 Node 层成立。
- `/interrupt` 是唯一能在命令循环卡住时打断的通道：Node 直接 POST，Python 侧 set `InterruptController`（`httpframe.py:125-136`；`cli.py:770-778`）。`InterruptController` 是 `threading.Event`，InputController 每个步骤与可中断 sleep 都 check（`interrupts.py:23-60`；`input.py:599-601,1730-1734`）。

### 3.8 PID 记录与单实例（`pidfile.py`）

- 记录落 `%LOCALAPPDATA%/dsh-vhcu/run/vhcu-<pid>.json`（`VHCU_PID_DIR` 可覆盖），内容含 version/pid/parentPid/createTime/image，临时文件 + `os.replace` 原子写（`pidfile.py:75-84,141-157`）。
- 启动时扫描目录，仅当「记录的 host 父进程已死」且「镜像名在允许集合」且「进程创建时间与记录完全一致」才 `TerminateProcess`；`ERROR_INVALID_PARAMETER` 视为进程已消失并清记录，`ACCESS_DENIED` 保持不动（`pidfile.py:19-28,175-275`）。父进程未知（0）一律视为存活（`pidfile.py:121-124`）——保守优先，避免多 profile 互杀。
- 没有强制单实例锁：同机多 DSH profile 可以各跑一个 daemon，靠 parentPid 区分（推断，符合注释意图 `pidfile.py:5-8`）。

---

## 4. Node 侧桥接

### 4.1 Python 解析（`host/lib/python.js`）

- 显式优先：`options.pythonPath ?? VHCU_PYTHON`；否则依次 `py -3`、`python`、`python3`（`python.js:13-29`）。
- 每候选先 `--version` 且必须以 `Python 3.` 开头，再跑 `-c "import sys;print(sys.executable)"` 拿真实路径（`python.js:32-58`）。全失败抛 `E_UNAVAILABLE` 并列出每个候选的失败原因（`python.js:60-66`）。
- `resolvePythonSync()` 故意未实现（`python.js:69-74`）：避免在宿主事件循环启动期阻塞。

### 4.2 Supervisor（`host/lib/supervisor.js`）

- 只拥有一个子进程；契约六条写在文件头（`supervisor.js:1-16`）。
- spawn：`detached:false`、`windowsHide:true`、`stdio:['pipe','pipe','pipe']`，附加 `--parent-pid process.pid --protocol 1`（`supervisor.js:335-359`）。
- 环境：在 `process.env` 上**硬删除** `ELECTRON_RUN_AS_NODE`、`NODE_OPTIONS`，设 `PYTHONUNBUFFERED/PYTHONIOENCODING/PYTHONUTF8`，把 `vision/` 前置进 `PYTHONPATH`，并注入三条绝对资源路径（`supervisor.js:397-413`）。
- READY 门：默认 15s（`supervisor.js:386-394`；常量 `contract.js:25`）。READY 时校验 `proto` 版本，不一致 `E_VERSION` 直接失败（`supervisor.js:479-503`）。
- 命令：pending Map + 每命令定时器，超时 reject `E_TIMEOUT` 并从 Map 删除；写失败立刻 reject（`supervisor.js:146-189`）。ack 到达后按 `ok` resolve/reject，携带 `code/hint/fatal/cmdId/op`（`supervisor.js:505-527`）。
- 心跳看门狗见 §1.4。
- 崩溃恢复：`_fail()` 记录失败窗口（默认 60s），窗口内第 3 次失败即 `circuit-open`，此后 `start()` 直接 `E_UNAVAILABLE`，只能 `restart()` 手动复位（`supervisor.js:567-616`；常量 `contract.js:29-32`）。
- 重启退避：`min(500ms · 2^(n-1), 30s)`，且**故意不 unref**——「等待重启」是真实待办，不能让宿主事件循环提前退出（`supervisor.js:618-635`，注释引 dsh-island P1-03 教训）。
- 停止：幂等，`stopPromise` 去重；先 shutdown 帧，3s 宽限，再 terminate，2s 宽限，仍不死才 kill tree（`supervisor.js:192-201,637-681`）。
- 身份安全：只对自己持有的 `ChildProcess` 发信号，且只在 `exitCode === null` 时；Windows 下用 `exitCode !== null || signalCode !== null` 判死（`supervisor.js:32-34,709-732`）。`stop(expectedStartedAt)` 拒绝误杀复用 PID（`supervisor.js:192-197`）。
- 宿主退出钩子：同步 `spawnSync(taskkill /T /F)`，因为异步 kill 在事件循环结束后跑不了（`supervisor.js:288-311`）；配合 Python 侧 `reclaim_orphans()` 形成双保险。
- stderr：按行切分，环形缓冲 200 行、每行截断 500 字符，发 `stderr` 事件（`supervisor.js:104-107,547-558`）。
- 主动中断：`interrupt()/clearInterrupt()` 走 loopback HTTP，1.5s 超时，不走可能被宏阻塞的 stdio（`supervisor.js:238-272`）。

### 4.3 行解码与拉帧

- `LineDecoder` 不无限缓冲：超 64 KiB 的行丢弃并报 `E_PROTO`；无换行且超预算时清空缓冲（`host/lib/protocol.js:10-55`）。
- `fetchFrame`：`127.0.0.1:<port>/frame?token&maxw&x&y&w&h&seq`，8 MiB 上限、2s 默认超时，非 200 抛 `E_CAPTURE`（`host/lib/frames.js:10-79`）。`fetchFrameMeta` 用 HEAD 只取 `x-frame-size` 等头（`frames.js:97-147`）。
- 组装点：`ensureSupervisor()` 用 `resolvePython()` + `new Supervisor({python, visionPath: VISION, daemonArgs:['-u','-m','vhcu_daemon'], cwd: ROOT, resourcePaths})`（`lib/index.js:1057-1074`）。

---

## 5. 打包与依赖

- PyInstaller spec：**onedir**（`COLLECT`），入口 `vision/vhcu_daemon/entrypoint.py`，`pathex=[vision]`，hiddenimports 只列 `drivers.windows/factory`、`paths`、`interrupts`；排除 tkinter/pandas/torch/tensorflow 等（`vision/vhcu_daemon.spec:15-65`）。注释明确「模型不打包、不做 onefile 解压到 %TEMP%」（`spec:1-9`）。
- 产物：`dist/vhcu-daemon/vhcu-daemon.exe` + `_internal/`，本机实测目录约 239 MB（只读 `du`，未运行）。
- 依赖：运行时 6 个直接依赖（`requirements.txt`：mss/numpy/soundcard/webrtcvad-wheels/faster-whisper/uiautomation），完整闭包在 `requirements.lock`（35 项）；离线安装用 `requirements-offline.txt`（`--no-index --find-links .\wheels`）。
- wheelhouse：`scripts/build-wheelhouse.ps1` 用 `pip download --only-binary --platform win_amd64 --python-version 3.13`，强制 CPython 3.13，输出 `manifest.json`（name/bytes/sha256）（`build-wheelhouse.ps1:8-62`）。
- 源码调试开关：`VHCU_PYTHON` 指向 venv python 即可；`resolvePython` 会跳过其他候选（`python.js:26-29`）。本机实际安装是软链：`~/.dsh/profiles/desktop/node_modules/dsh-vhcu -> <workspace>/dsh-vhcu`（只读 `ls -l`），即当前运行的是源码模式。
- **重要事实**：仓库 Node 代码里没有任何地方选择 `dist/vhcu-daemon/vhcu-daemon.exe`；`Supervisor` 永远 spawn Python 解释器 + `-m vhcu_daemon`（`host/lib/index.js:1061-1068`、`host/lib/supervisor.js:65,335-344`，全仓 grep `vhcu-daemon.exe` 只命中 pidfile 允许名单、spec、RELEASE_NOTES/README）。RELEASE_NOTES 的 "Engine: dist/vhcu-daemon/vhcu-daemon.exe"（`RELEASE_NOTES.md:5`）与运行时装配不一致；是发布脚本另有动作，还是文档超前，需要发布负责人确认（见 §7、§9）。

---

## 6. Python 测试

- 入口：`py -3 -m unittest discover -s vision/tests -t vision -v`（`package.json` 的 `test:py`）；也可 `npm run test:py`。
- 13 个测试模块，静态计数 **258 个 `def test_`**（未执行，仅 grep 计数；不作为通过证据）：
  `test_input.py` 72、`test_hear.py` 37、`test_capture.py` 23、`test_units.py` 23、`test_element_registry.py` 20、`test_uia_worker.py` 19、`test_privacy.py` 13、`test_virtual_cursor.py` 12、`test_human_input.py` 11、`test_platform.py` 11、`test_pidfile.py` 8、`test_cursor_overlay.py` 6、`test_shapes.py` 3。
- 覆盖面（按文件）：
  - `test_units.py`：FakeSource、diff、PNG 编码、Protocol/parse_line、HttpFrameServer、parent watchdog、Runner 捕获、帧缩放；
  - `test_capture.py`：mss origin、质量检测、GDI 回退顺序、遮挡/最小化拒绝、隐私帧缓存清理、overlay 隐藏/恢复、frame_seq 近期帧；
  - `test_platform.py`：绝对路径、InterruptController、驱动工厂、HTTP interrupt、safety 纯函数；
  - `test_input.py` / `test_human_input.py` / `test_shapes.py`：注入参数校验、dry-run、humanize 轨迹、形状生成；
  - `test_uia_worker.py` / `test_element_registry.py`：请求归一化、json_safe、指纹/stale/错误分类；
  - `test_virtual_cursor.py` / `test_cursor_overlay.py` / `test_privacy.py` / `test_pidfile.py` / `test_hear.py`：各类边界与生命周期。
- 另有 `vision/test_hear_standalone.py`（手动跑 Hear 的脚本，不在 tests 目录）与 `vision/test_uiohook.js`（Node 侧 uiohook POC）。
- Node 测试：`npm test` = `node --test contract/test/*.test.mjs host/test/*.test.mjs`（`package.json`）。

---

## 7. 文档 / 任务书声明 vs 代码事实

| 声明 | 代码事实 | 影响 |
|---|---|---|
| README：帧由「short-lived signed URLs」提供（`README.md:88`） | 签名+5 分钟过期在 Node 层（`lib/index.js:546-572`）；daemon 侧只是永久 token（`httpframe.py:148-150`） | 直接访问 daemon 端口的人不受 TTL 约束 |
| README：NDJSON + 「monotonic sequence numbers and absolute deadlines」（`README.md:87`） | 有实现，位置见 §1.3；但 Python 不校验入站 seq 连续性，gap 只由 Node 报 `E_SEQ` | 声明成立，实现边界更窄 |
| RELEASE_NOTES：Engine = `dist/vhcu-daemon/vhcu-daemon.exe`（`RELEASE_NOTES.md:5`） | 无代码选择该 exe，Supervisor 只 spawn Python（`host/lib/index.js:1061-1068`） | 可能是文档超前或发布包另有补丁，见 §9 |
| 本任务书：命令面含 `record.*` / `macro.*` | `record.*` 在 Node（uiohook，`lib/index.js:4895-4915`）；daemon 只有 `input.macro.*` | 文档写作需纠正 |
| `Runner.capture_now` docstring：「raise a fatal E_PRIVACY_BLOCKED」（`runner.py:459-464`） | 实际返回合成隐私占位帧并带 `privacyBlocked=true`（`runner.py:471-487`） | 注释陈旧；但 Node 路由在 `state.privacy.blocked` 时直接 403（`lib/index.js:1719-1721`），行为仍安全 |
| 契约 37 个错误码（`contract.js:72-185`） | Python 白名单 29 个（`protocol.py:21-51`），部分码降级 `E_INTERNAL` | 提示/重试精度下降 |
| `--source` 仅 `fake|mss`（`cli.py:998`） | `create_source` 实际支持 `window`（`sources.py:185-195`） | 独立 CLI 无法选 window，仅 `vision.capture targetHwnd` 路径可用 |
| 任务书：`safety.py` 急停角 | `safety.py` 是纯函数；真正注入侧实现是 `input._failsafe`（`input.py:1719-1728`）+ `CORNER_PX=8` | 两层实现，语义一致 |
| `MAX_ROIS` 契约 8（`contract.js:22`） | `diff.compare(max_rois=8)` 默认一致（`diff.py:56`） | 一致 |

---

## 8. 升级敏感点（宿主 0.2.1-rc3 → 0.2.2-rc5）

> 判断依据只来自本仓库；宿主 0.2.2-rc5 的内部契约未在本文核对。按「一旦变化就会坏」排序。

1. **插件 manifest 版本闸门**：`dsh.plugin.json` 的 `compatibility.dshReleases` 只列到 `0.2.1-rc.3`（`dsh.plugin.json:8-12`），`package.json` `engines.dsh` 是 `>=0.1.5-rc.1 <0.3.0-0`（`package.json`）。若 0.2.2-rc5 严格按 `dshReleases` 白名单判定，插件会被判为不兼容；若只按 semver 区间则通过。**升级前应先确认宿主用哪一份，并决定是否补 `0.2.2-rc.5` 条目**（本仓库未改动）。
2. **Python 解释器与依赖仍在宿主用户机上**：运行时装配是 `resolvePython()` + `-m vhcu_daemon`（`host/lib/index.js:1060-1068`），不是 bundled exe。升级宿主不会带上 Python 3.13/6 个依赖；若新宿主换了内置 Node/Python 或改了 PATH，`resolvePython` 的 `py -3` 探测可能落到错误解释器。`VHCU_PYTHON` 仍是最稳的显式开关（`python.js:26-29`）。
3. **`webServer` 契约**：`frameUrl` 依赖 `ctx.webServer.getOrigin() / .origin / .port` 三种形态，任一缺失就退化为相对 URL（`lib/index.js:530-540`）；`/vhcu/frame` 路由通过 `ctx.webServer.register`（`lib/index.js:1755-1762`）。0.2.2 若改 webServer 的 origin/register 语义，帧链接会 404/空 host，而控制面（stdio）仍正常——表现为「工具能用但图看不到」。
4. **插件生命周期 API**：`ctx.effect(...)` 用于挂/卸路由（`lib/index.js:1756-1761`），`inject: ['webServer','tools']`（`lib/index.js:67`）。宿主若改 effect/tool registry 签名，插件 mount 阶段就会失败，daemon 根本不会 spawn。
5. **profile 环境变量**：状态目录按 `DSH_PROFILE_KEY / DSH_PROFILE / DSH_PROFILE_DIR` 取哈希做隔离（`host/lib/paths.js:30-38`）。0.2.2 若改名/移除这些变量，所有 profile 会退回共享 `%LOCALAPPDATA%/dsh-vhcu`，造成设置/宏/断点互相串（不是崩溃，是数据隔离回归）。
6. **进程模型 / stdio**：Supervisor 假设它能拿到真实 pipe（`stdio:['pipe','pipe','pipe']`，`supervisor.js:349-355`），`--parent-pid` 用 `process.pid`（`supervisor.js:337-344`），退出钩子用 `process.once('exit')`（`supervisor.js:310`）。若 0.2.2 把插件放进 utilityProcess/worker 或吞掉 fd 1/2，会表现为 READY 超时或心跳立即丢失；watchdog 仍能兜底杀 daemon，但会反复重启直到 breaker。
7. **工作目录**：`cwd: ROOT`（`lib/index.js:1065`），Python 侧只依赖 `PYTHONPATH`（`supervisor.js:406`）与绝对 `VHCU_*`（`paths.py:31-61`），不依赖 cwd；因此宿主改 cwd 风险低。但**若插件被装进 `app.asar`**，`join(HERE,'..','vision')` 将指向 asar 内路径，Python 无法 `-m` 导入——当前安装是 profile 下的软链（源码目录），升级时要确认安装形态仍是解包目录。
8. **环境变量卫生**：Supervisor 删除 `ELECTRON_RUN_AS_NODE`/`NODE_OPTIONS` 并强制 `PYTHONUTF8=1`（`supervisor.js:401-405`）。0.2.2 若通过 `NODE_OPTIONS` 注入必要 polyfill，删除它可能影响其他插件；对 VHCU 自身是有益的。
9. **可选的 `uiohook-napi`**：录制/在场检测是 optionalDependency（`package.json`），升级宿主（Electron ABI 变化）后预编译 `.node` 可能失配；失配只影响录制与 presence，不影响 daemon 桥（`lib/index.js:3612-3623,3860-3880`）。
10. **隐私闭环依赖宿主侧状态**：截图路由在 `state.privacy.blocked` 时 403（`lib/index.js:1719-1721`），该状态来自 daemon 的 `vision.blocked` 事件（`runner.py:205-232`）。这只是「多一层」，Python 侧已独立 fail-closed（`input.py:415-441`）；升级时不要只测 Node 层。
11. **错误码降级**（§2）：升级后若宿主按 `E_FOCUS_LOST / E_MACRO_FAILED` 做分支，实际收到 `E_INTERNAL` 会走错分支。建议在升级回归中显式覆盖 4 个被降级的码。
12. **`E_MEMORY` 事件无消费者**：daemon 会在硬水位发 `daemon.memory_watermark`（`cli.py:264-303`），但 Node `lib/index.js` 没有对应 handler（grep 无命中）；Node 另有 `MEMORY_LIMIT_BYTES` 1.5 GiB 硬熔断（`host/lib/guards.js:22`；软水位常量见 `lib/index.js:97-102`）。升级不改变这一点，但别误以为 Python 水位会触发宿主侧重启。

---

## 9. 未读透 / 疑问

1. **发布引擎到底走哪条路**：`dist/vhcu-daemon/` 存在且 RELEASE_NOTES 宣称它是 Engine，但仓库内无任何代码选择 exe。是发布时另有 `scripts/install.mjs` 之外的安装器替换，还是文档超前？需要在发布机器上核对 `install.mjs` 的实际行为与 zip 布局（本次只读仓库，未执行安装脚本）。
2. **Python 错误码白名单为何没同步**：`protocol.py:21-51` 比 `contract.js:72-185` 少 8 个码，导致 `E_FOCUS_LOST/E_CLIPBOARD/E_AUDIO_DEVICE/E_MACRO_FAILED/E_CONFIG` 被 `ProtocolError` 吞成 `E_INTERNAL`。这是历史遗漏还是刻意降级？未找到注释或测试覆盖该映射（`test_platform.py` 只测 Interrupt/路径等）。
3. **`capture_now` 注释与实现矛盾**：docstring 说隐私时抛 fatal `E_PRIVACY_BLOCKED`，实现返回占位图。两者对模型行为影响不同（前者会中断任务，后者可能被当成「截到了」）。P22.4 的真实意图需作者确认。
4. **UIA 请求跨线程携带回调**：`uia.click_point` 把 `privacy_check` 可调用对象放进 job 的 target dict，由 COM 线程执行（`cli.py:493-498`；`uia_worker.py:731-741`）。当前回调只读窗口标题（`cli.py:854-860`），不碰 COM，所以没看到问题；但「COM 对象不出线程」的约束没有覆盖「任意回调可进线程」，属于契约未声明。
5. **虚拟光标 action 参数**：`_virtual_click` 里 `self._animate_safe(x, y, humanize_profile, "click" if action == "click" else "click")`（`input.py:692`）三元两端相同，导致 double/right/middle 也一律按 click 着色。可能是有意（只区分点击与非点击），也可能是笔误；`ACTION_COLORS` 里 `drag` 在 virtual 点击路径永远用不到。
6. **token 无过期**：daemon 的 `?token=` 在进程生命周期内恒定（`cli.py:134`），Node 层 TTL 无法约束直接访问 loopback 端口的同用户进程。是否需要在 daemon 侧也加过期/轮换，未在注释中讨论。
7. **`MAX_CMD_QUEUE=256` 常量未被使用**：`contract.js:21` 定义了它，但 Node 无队列、Python 顺序读，实际背压由管道与 per-command timeout 承担。是否有意保留？
8. **`hear.py` 的 `_SHORT_PHRASES` 与 `ABSOLUTE_BLACKLIST` 命中率**：这些是经验阈值/短语，未在本次静态调研中评估其误杀/漏杀；要给出质量结论必须实跑（本轮禁止）。
9. **宿主 0.2.2-rc5 的具体差异**：本文无法核对宿主的 `webServer`、`ctx.effect`、`DSH_PROFILE*` 在 0.2.2-rc5 是否变化。升级敏感点是「条件风险」，需要宿主侧调研文档交叉验证（若同目录另有 01/02 篇，应以那两篇为准）。
10. **测试是否真的全绿**：本轮按要求未执行 Python 测试，只做了静态计数（258 个 test 函数）。memory 中「Python 88/88」与静态计数口径不同，不能当作本版本的通过证据。

---

### 附：一句话总结

Python 守护进程是一个「stdio NDJSON 控制面 + loopback 图像面」的双通道 worker：协议层用 seq 与绝对 deadline 防乱序/幽灵动作，用 fd 劫持保证 stdout 只走帧；能力层把 Windows 捕获/差分/注入/UIA/内录/虚拟光标全部关在本进程内，隐私与前台焦点在注入前 fail-closed；Node 侧 Supervisor 负责解释器发现、生命周期、心跳看门狗、退避与熔断。对宿主升级真正敏感的不是 Python 代码本身，而是**插件被如何装载**（manifest 闸门、webServer/effect API、profile 环境变量、stdio 与安装形态）以及**错误码契约在两侧是否仍然一致**。
