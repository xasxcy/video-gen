# 计划：接入 Gemini Omni Flash 视频生成

状态：已完成
关联调研笔记（完整 API 核实细节，实现前必读）：`/Users/xasxcy/.hermes/profiles/secretary/workspace/notes/video-gen-omni-flash-integration-2026-08-04.md`

## 背景

`video-gen` skill 目前只接了 Veo（`video_gen.py`，走 `predictLongRunning`）。现在追加接入 Google Omni Flash（`gemini-omni-flash-preview`），它走的是完全不同的 Interactions API。已核实：当前 GCP 项目（`project-53260abd-312a-4cb0-aa0`）对该模型可直接调用，无配额障碍（泽豪实测 curl 成功）。

## 架构决策（已与用户确认，不要重新讨论）

新增**独立脚本** `video_gen_omni.py`，不并入 `video_gen.py` 的 `--model` 分支。原因：两者请求体结构、能力边界（分辨率/时长/音频开关/多轮编辑机制）差异太大，硬塞一起会产生大量条件分支和"仅某模型支持"的脚注。

只共用一小块：GCP 认证/access token 获取逻辑。做法：把 `video_gen.py` 里的 `load_env_file()` 和 `get_access_token()` 抽到新文件 `_auth.py`，两个脚本都从它导入。`video_gen.py` 改成从 `_auth` 导入这两个函数（删除原地定义），保持行为完全不变。

## 关键 API 事实（详见调研笔记，这里只列写代码要用到的）

- Endpoint: `POST https://aiplatform.googleapis.com/v1beta1/projects/{PROJECT_ID}/locations/global/interactions`
- 认证：与 Veo 相同的服务账号 Bearer token（`_auth.get_access_token()` 直接复用）
- 模型 ID：`gemini-omni-flash-preview`
- 请求体（text-to-video）：
  ```json
  {
    "model": "gemini-omni-flash-preview",
    "input": [{"type": "text", "text": "..."}],
    "response_format": [{
      "type": "video", "delivery": "uri", "gcs_uri": "...",
      "aspect_ratio": "16:9|9:16", "duration": "3s"~"10s"
    }],
    "generation_config": {"video_config": {"task": "text_to_video"}}
  }
  ```
- image-to-video：`input` 数组追加 `{"type": "image", "mime_type": "image/png", "data": BASE64}`，`task` 改为 `"image_to_video"`
- 异步：加 `"background": true`；轮询用 `GET https://aiplatform.googleapis.com/v1beta1/{interaction_name}`（`interaction_name` = 响应里的资源名，形如 `projects/P/locations/global/interactions/ID`）—— **这条是按 REST 惯例推断，未逐字见过官方 curl 示例，实现时第一次调用要核实真实字段名，如与推断不符要在笔记里更正。核实方式见步骤 5 的日志安全约束——只能把响应写入受限权限的临时文件后离线核对，禁止打印完整响应到终端**
- 时长上限 10 秒，分辨率固定 720p，视频输出固定带音频（无 `--audio`/`--no-audio` 开关）
- 计费：$0.10/秒视频输出（720p 含音频）+ 输入/输出 token 费用（文本/图片/音频/视频输入 $1.50/百万 token，文本输出 $9/百万 token）—— 比 Veo 多一层 token 计费，`--force`/预检查文件已存在这类"别浪费已花的钱"的保护逻辑要照抄 `video_gen.py` 的做法

## 实现步骤

1. **抽取共享认证模块**：新建 `_auth.py`，把 `video_gen.py` 里的 `load_env_file()`、`get_access_token()`、`SCOPES` 常量搬过去；`video_gen.py` 改为 `from _auth import load_env_file, get_access_token, SCOPES`。跑一遍现有 `tests/test_video_gen.py` 确认没有破坏 Veo 的行为。
   - 顺手修一个现存小问题：`video_gen.py` 第 14-15 行 `import time` 重复了两次,抽取时去掉多余的一行。
2. **新建 `video_gen_omni.py`**，CLI 参数参考 `video_gen.py` 的结构但按 Omni 实际能力精简：
   - 位置参数 `prompt`，`--image`（本地文件，base64 内联，非 GCS）
   - `-o/--output`，`--aspect-ratio`（16:9/9:16，无默认强制推断时留空由 API 自己推断，或沿用 9:16 默认以保持两个脚本习惯一致——待实现时权衡，不是硬约束）
   - `--duration`：3-10 的整数，内部转成 `"Ns"` 字符串
   - `--storage-uri`（对应 `gcs_uri`，用法与 Veo 一致）
   - `--force`（复用 `video_gen.py` 的 `_write_exclusive`/`_write_atomic_force` 防覆盖逻辑，可以整体照搬这两个函数，或者也一并抽到共享模块——实现时看代码量决定，别为了复用两次的代码过度抽象）
   - `--background`（对应异步模式）、`--poll-interval`、`--timeout`
   - `--interaction`（**Codex 评审第 3 条采纳**：恢复模式——传入之前一次提交拿到的 interaction 资源名，跳过 `submit_interaction`，直接进入 `poll_interaction`/取结果，用于本地进程超时/中断后，已提交且已付费的后台任务仍能找回结果；异步保留期是 14 天，见调研笔记）
   - `--project`/`--location`（**注意默认 location 是 `global`，不是 `video_gen.py` 默认的 `us-central1`**）/`--credentials`
   - 不需要：`--resolution`（固定 720p）、`--audio`/`--no-audio`（固定带音频）、`--last-frame`（Omni 目前没有首尾帧插帧）、`--sample-count`（Omni 每次一个 interaction 产出的视频数量传参方式还没查清楚，先只支持生成 1 个，不支持就在 `--help` 里不出现这个选项，别加一个实际不生效的假选项）
3. **核心函数**（对应 `video_gen.py` 的 `build_request_body` / `submit_generation` / `poll_operation` / `save_videos`）。**错误处理统一约束**：任何抛出的异常/打印的错误信息，禁止把 HTTP 响应体（`resp.text`/`resp.json()`）整段拼进消息里——`video_gen.py` 现有 Veo 代码里 `submit_generation`/`poll_operation` 这么做过，那是因为 Veo 的错误响应体通常很小；Omni 的响应体可能带内联视频数据，同样的写法会把大段内容甩进异常信息/日志。改成只截取状态码 + 错误信息里明确是 `error`/`message` 字段的那部分（如果解析失败就只打印长度和状态码，别整体截断打印，截断不代表安全，截断前几百字节仍可能是半个 base64 视频片段）：
   - `build_interaction_body(args) -> dict`
   - `submit_interaction(project, token, body) -> dict`（同步模式直接拿到完整结果；异步模式返回带 `id`/资源名的初始对象）——**提交成功后立刻把返回的 interaction 资源名打印到 stderr（格式参考 Veo 现有 `operation_name` 的打印方式），这是 `--interaction` 恢复模式能工作的前提，不能等轮询/保存都做完才打印**
   - `poll_interaction(project, token, interaction_name, poll_interval, timeout) -> dict`（异步模式和 `--interaction` 恢复模式共用；要处理 `status` 为 `failed` 的分支，不能只判断 `completed`）
   - `save_video(payload, output, force) -> Path`（响应体结构和 Veo 不同，视频在 `steps` 里 `type: model_output` 的 `content` 数组里，字段是 `uri`（gcs）或可能的 inline base64——**实现时不要凭调研笔记里那段示例 JSON 假设死，以步骤 5 真实调用后离线核实到的形状为准（核实过程遵守步骤 5 的日志安全约束，不打印完整响应到终端）**）
4. **写测试** `tests/test_video_gen_omni.py`，覆盖（**注意：这一步先写基于假设形状的测试骨架，第 5 步拿到真实 fixture 后要回来补全/修正，不是写完这一步就算数**）：
   - `build_interaction_body` 纯文本 / 带图片两种情况
   - duration 校验（3-10 范围外报错）
   - `save_video` 处理 gcs uri 和潜在的 base64 内联两种响应形状
   - `poll_interaction` 处理 `completed`/`failed`/超时三种分支
   - `--force`/已存在文件的保护逻辑（可直接复用 `video_gen.py` 现有测试用例的写法）
   - `--interaction` 恢复模式：给定一个资源名，跳过 submit 直接进入 poll
   - 用 mock/monkeypatch 打桩网络请求，不要在测试里真的发 HTTP 请求
5. **实测真实调用 —— 已获泽豪批准直接执行，不是可选步骤**（Codex 评审第 1 条采纳：mock 无法验证 Interactions 协议本身，最坏情况是用户已提交并付费的后台任务因轮询/解析错误无法取回，所以这一步是合并前的硬性验收条件，不能只用 mock 测试替代）。至少覆盖两条路径，都要跑真实请求：
   - **同步路径**：一次最小 `text_to_video` 请求（3 秒，最短最省钱）。
   - **异步路径**：一次 `background:true` 请求，拿到 interaction 资源名后，用推断出的 `GET https://aiplatform.googleapis.com/v1beta1/{interaction_name}` 去轮询，确认这个 GET 端点和字段名是否与调研笔记里的推断一致——这条目前完全没有官方 curl 示例佐证，是整个计划里风险最高的一处假设，必须靠这次真实调用验证，不能留到用户报 bug 才发现。
   - **日志安全约束（Codex 评审第 3 轮采纳）**：完整原始响应可能包含大段内联 base64 视频数据或带签名的下载 URL。**任何时候都不允许把完整原始响应打印到 stdout/stderr 或留在会话记录/终端截图里**（脱敏是"事后"操作,数据一旦打进日志就已经泄露了,补救不了）。正确做法：
     1. 把两次调用的原始响应各自写入一个受限权限的临时文件（如 `chmod 600`，存在 `/tmp` 或系统临时目录，不落在仓库里）。
     2. 只在终端里打印/记录一份**结构摘要**：顶层 key 列表、每个 value 的类型、字符串长度（不打印内容本身）、视频字段用的是 `uri` （打印 scheme 如 `gs://...` 前缀即可,不打印完整签名 URL）还是内联 base64（只打印字节长度,不打印内容）。
     3. 基于临时文件里的原始响应，手工/脚本生成脱敏后的 fixture（视频内容替换成占位字符串或截断到几十字节，保留结构和字段名），存到 `tests/fixtures/omni/`。
     4. fixture 生成完毕后，删除步骤 1 的临时文件。
   - 第 4 步的单测基于这份脱敏 fixture 写，而不是凭空编造的 mock 数据结构。
   - 验证完成后，把确认后的真实字段形状回写进调研笔记（把"按 REST 惯例推断"这类措辞改成"已实测确认"或纠正）——**回写笔记时同样只写结构摘要，不粘贴原始响应全文**。
6. **更新 `SKILL.md`**：新增一节说明 Omni Flash 的用法、限制（10 秒上限、720p、无插帧）。**v1 范围明确不包含多轮对话式编辑（`steps` 拼接）和 reference_to_video（参考图驱动的主体一致性重生成）**——调研笔记"结论"部分把这两项列为选择 Omni 的理由之一，但本次实现（步骤 2-3）没有设计这两个能力所需的会话状态持久化和请求体，直接照抄笔记结论会让文档承诺一个 CLI 实际不支持的能力（Codex 评审第 2 条采纳）。SKILL.md 里"什么时候用 Omni"只能写"需要 10 秒内短视频"和"用参考图做 image-to-video 首帧驱动"，多轮编辑/reference_to_video 留作后续迭代，不写进本次文档。同时把文档里"Vertex AI"的措辞顺手更新为提一句"现已更名为 Gemini Enterprise Agent Platform"（不用全文替换，加一句注解即可）。
7. **更新 `pyproject.toml`**：`[project.scripts]` 加一条 `video-gen-omni = "video_gen_omni:main"`；`include` 列表加上 `video_gen_omni.py` 和 `_auth.py`。
8. **README.md** 如果提到了"只支持 Veo"之类的表述，一并更新。

## 收尾（缺一不可，参照 dev-cycle 军规）

- [ ] `uv run pytest` 全绿（新增 + 原有测试）
- [ ] codex-adversarial-loop 或 subagent code review 收敛
- [ ] 文档（SKILL.md / README.md / pyproject.toml）同步更新
- [ ] git 提交：代码一次提交（`feat: ...`），文档一次提交（`docs: ...`），分开提交
- [ ] 提交前检查暂存内容没有 `.env`、密钥等敏感文件

## 执行建议

按 dev-cycle 军规，实际编码/测试尽量派给 subagent 执行，主对话只做计划确认和最终评审。建议分两个 subagent 任务：
1. 步骤 1-4（抽取认证模块 + 写 `video_gen_omni.py` + 基于假设形状的单元测试骨架，全部用 mock，不发真实请求）
2. 步骤 5（真实请求验证，已获批准，直接跑，含同步 + 异步两条路径，回填 fixture 和修正测试）之后再做步骤 6-8（文档 + 提交）
