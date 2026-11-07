# VHCU P17.2 测试执行指南

**目标**：验证 AI 在真实桌面里完成「视觉操作 → 保存专精宏 → 关窗重开 → 按当前窗口推荐宏 → 秒放新题」的完整闭环。

**测试对象**：Windows 计算器（Calculator / 计算器）+ VHCU 插件。

**预计耗时**：5–8 分钟（不含视觉模型响应等待）。

---

## 0. 测试前准备

1. 完全退出 DSH，再重新打开（确保加载最新 VHCU 代码）。
2. 打开 VHCU 面板。
3. 点击「开启视觉」。
4. 确认面板顶部：
   - `源` 有值；
   - `pid` 有值；
   - `fps` 显示 `-- 等待中` 或实际帧率；
   - 右上角有「关闭视觉」「紧急制动」「⚙」。
5. 打开一个计算器窗口（先不要让它抢焦点，下一步让模型自己开）。

> 如果测试中 AI 失控，点击「紧急制动」或按 `Ctrl+Shift+X`。
> 这不会影响本测试的宏文件，但会结束当前视觉/听觉进程。

---

## 1. 第一步：视觉打开计算器并计算 888 × 666

把下面整段发给测试对话里的模型：

```text
请用 VHCU 完成一个真实桌面任务：

1. 用 vhcu_start_vision 开启视觉（如果已经开启就跳过）。
2. 打开 Windows 计算器（按 Win+R → 输入 calc → 回车；不要用 macro_launch_app 配空 text，避免空 type 步骤报错）。
3. 用 vhcu_get_active_window 读取计算器窗口标题，记下它（可能是 "Calculator" 或 "计算器"）。
4. 用 vhcu_get_full_frame 截一张全屏，视觉定位计算器的数字键和运算符键。
5. 用 vhcu_mouse_click 依次点击：8、8、8、×、6、6、6、=。
6. 用 vhcu_get_vision_events 或 vhcu_get_full_frame 回读，确认计算器显示 591408。
7. 把实际点击的坐标、每一步耗时、回读结果告诉我；不要保存宏，先等我确认。
```

### 预期结果

- 计算器窗口被打开；
- 屏幕显示 `591408`（888×666 = 591408）；
- 模型报告了：
  - 计算器窗口标题；
  - 数字键/乘号/等号的坐标或点击步骤；
  - 至少一次截图回读；
- 没有误点其他窗口。

### 如果失败

| 现象 | 处理 |
|---|---|
| 找不到计算器 | 手动打开计算器，再让模型从第 3 步继续 |
| 点错数字 | 让模型按 `Esc` 清空后重试，最多 2 次 |
| 视觉截图没返回 | 检查 `vhcu_start_vision` 是否成功；重试一次 |
| 结果不是 591408 | 让模型报告实际显示值，不要继续保存宏 |

---

## 2. 第二步：把动作序列保存成参数化专精宏

确认 591408 正确后，把下面整段发给模型：

```text
现在把刚才的计算流程保存成可复用宏，要求：

- 宏名：macro_calc_multiply
- 说明：计算 a × b（Windows 计算器）
- 分类：我的自定义宏
- target_app：填刚才 vhcu_get_active_window 返回的标题里的应用名，
  必须是 "Calculator" 或 "计算器"，不要填整段标题。
- 参数：a、b
- 步骤必须参数化，不要保存带绝对坐标的数字键 click，否则新题会点错。
  推荐 steps：
    1. key esc（清空计算器）
    2. type {{a}}
    3. type *
    4. type {{b}}
    5. key enter
    6. wait 300
- 调用 vhcu_save_macro 保存，保存后把返回的宏 JSON 和变量列表发给我。
- 不要立刻执行新题，先等我确认。
```

### 模型应调用的工具（核对用）

```json
{
  "name": "macro_calc_multiply",
  "description": "计算 a × b（Windows 计算器）",
  "category": "我的自定义宏",
  "target_app": "Calculator",
  "steps": [
    { "op": "key", "keys": ["esc"] },
    { "op": "type", "text": "{{a}}" },
    { "op": "type", "text": "*" },
    { "op": "type", "text": "{{b}}" },
    { "op": "key", "keys": ["enter"] },
    { "op": "wait", "ms": 300 }
  ]
}
```

如果计算器标题是中文，则：

```json
"target_app": "计算器"
```

### 预期结果

- 返回 `ok: true`；
- `macro.name === "macro_calc_multiply"`；
- `macro.target_app` 是 `"Calculator"` 或 `"计算器"`；
- `variables` 为 `["a","b"]`；
- ⚙ 设置 → 宏管理 → 我的自定义宏 里能看到它；
- 宏的步骤里没有绝对坐标 click（只有 key/type/wait）。

### 如果失败

| 现象 | 处理 |
|---|---|
| `E_CONFLICT` 重名 | 先在 ⚙ 宏管理删除旧 `macro_calc_multiply`，再保存 |
| target_app 填了整段标题 | 改成标题中的 `Calculator` / `计算器` |
| 步骤里全是 click 坐标 | 让模型重存为上面的 key/type 参数化版本 |
| variables 为空 | 检查 type 步骤是否写成 `{{a}}` / `{{b}}` |

---

## 3. 第三步：关闭、重开、验证推荐并秒算 123 × 456

把下面整段发给模型：

```text
现在做闭环验证：

1. 关闭当前计算器窗口（用 macro_close_active_window，或手动关闭）。
2. 重新打开计算器（按 Win+R → 输入 calc → 回车；不要用 macro_launch_app 配空 text）。
3. 调用 vhcu_get_active_window，确认前台窗口是计算器，记录标题。
4. 调用 vhcu_get_recommended_macros，检查返回的 macros 里是否有 macro_calc_multiply，
   且 target_app 与当前窗口标题匹配。把推荐列表和 active_window 发给我。
5. 如果推荐里有，调用：
   vhcu_run_macro({
     name: "macro_calc_multiply",
     variables: { a: "123", b: "456" }
   })
6. 用 vhcu_get_full_frame 回读计算器，确认显示 56088。
7. 报告：推荐是否命中、宏执行耗时、最终显示值、截图。
```

### 预期结果

- 计算器重新打开后，`active_window` 是 Calculator / 计算器；
- `vhcu_get_recommended_macros`：
  - `ok: true`；
  - `active_window` 是计算器标题；
  - `macros` 包含 `macro_calc_multiply`；
  - 该宏排在通用宏之前或至少被明确列出；
- `vhcu_run_macro` 返回 `ok: true`；
- 计算器最终显示 `56088`；
- 整个宏执行耗时通常是几百毫秒到 1 秒（不含视觉截图）；
- 宏执行后不依赖数字键的绝对坐标。

### 如果失败

| 现象 | 处理 |
|---|---|
| 推荐列表没有该宏 | 检查 target_app 是否与窗口标题子串匹配（大小写不敏感）；中文标题要用“计算器” |
| 推荐有但执行报 `E_DENIED` | 宏被禁用：⚙ 宏管理里启用 |
| 执行报 `E_NOT_FOUND` | 宏没保存成功，重做第二步 |
| 显示值不是 56088 | 检查 steps 是否为 esc → {{a}} → * → {{b}} → enter；让模型回读实际显示 |
| 点击旧题的 888/666 坐标 | 说明保存了 click 步骤，重存参数化版本 |

---

## 4. 通过标准（全部满足）

- [ ] 第一轮视觉点击算出 `591408`；
- [ ] `macro_calc_multiply` 保存成功，`target_app` 为 `Calculator` 或 `计算器`；
- [ ] 宏变量为 `a`、`b`，步骤无绝对坐标；
- [ ] 关窗重开后，`vhcu_get_recommended_macros` 推荐出该宏；
- [ ] `vhcu_run_macro` 用 `a=123,b=456` 算出 `56088`；
- [ ] 整个过程没有重启 DSH；
- [ ] 失败时没有误点其他窗口，或紧急制动可用。

---

## 5. 测试记录模板

测试结束后，把下面这段填好发我：

```text
P17.2 测试结果
- 计算器窗口标题：
- 第一轮 888*666 结果：
- macro_calc_multiply 保存：是/否
- target_app：
- variables：
- 关窗重开：是/否
- 推荐是否命中：是/否
- 推荐列表（名称/target_app）：
- 123*456 执行结果：
- 宏执行耗时：
- 是否重启 DSH：是/否
- 异常/截图：
```

---

## 6. 备注

1. **为什么不用绝对坐标 click 保存宏**：计算器窗口位置、大小、DPI 可能变化，且新题数字不同；参数化 `type {{a}}` / `type *` / `type {{b}}` 才能秒放新题。
2. **target_app 匹配**：`vhcu_get_recommended_macros` 用当前窗口标题做不区分大小写的子串匹配。标题是 `Calculator` 就填 `Calculator`，是 `计算器` 就填 `计算器`；不要填整段标题。
3. **快捷键**：`Ctrl+Shift+X` 仅在 DSH 窗口聚焦时生效；紧急制动会 `taskkill /F /IM python.exe`，可能影响其他 Python 程序。
4. **测试期间不要**手动删除 `macros.json`；宏保存在用户目录的 VHCU 数据目录，重开 DSH 后仍在。
5. **如果模型保存了 click 版本**：先在 ⚙ 宏管理删除，再用第二步的 JSON 重新保存；否则新题会点到旧数字。
