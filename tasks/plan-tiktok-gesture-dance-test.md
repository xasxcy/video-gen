# PLAN: video-gen 首次真实调用验证 —— 生成 TikTok 手势舞视频

## 背景 / 目标

`video-gen` CLI（Veo on Vertex AI，`predictLongRunning` REST 接口）已完成脚手架并通过单元测试
（12/12，纯逻辑、无网络调用）、已推送到 https://github.com/xasxcy/video-gen 。

单元测试无法验证：
1. service account 换 access token 的真实鉴权是否成功
2. 请求体 schema 是否被 API 接受（尤其是 `image.bytesBase64Encoded` / `durationSeconds` 等字段名，
   来自官方文档但未经真实调用验证）
3. 响应体 schema 的假设是否正确（`response.videos[].bytesBase64Encoded` vs `gcsUri`，`fetchPredictOperation`
   的轮询行为）
4. 生成质量：用两张候选头像图之一生成一条 TikTok 风格手势舞视频，效果是否可用

本计划覆盖从"最小烟雾测试"到"最终可用视频"的执行步骤，每步都有明确的成功判据和成本上限。

## 候选输入图

- `/Users/xasxcy/.hermes/profiles/secretary/workspace/mika-base-v1/assets/pro-v2/expression-pack/candidates/DARK-FACE-SHAPE-001-DRESS-005-fixed-v2.png`（1792x2400，3:4，4.9MB）
- `/Users/xasxcy/.hermes/profiles/secretary/workspace/mika-base-v1/assets/pro-v2/expression-pack/candidates/DARK-FACE-SHAPE-001.png`（896x1200，3:4，1.4MB）

两张都是 3:4 竖版，不是 Veo 要求的 9:16/16:9——server 端会居中裁剪或按 `--resize-mode` 处理。先用
`DARK-FACE-SHAPE-001.png`（体积更小、更快上传）测试，如果裁剪把脸切了再切到另一张或做预处理。

## 成本预算

默认模型 `veo-3.1-lite-generate-001`，纯视频（无音频），720p = $0.03/秒。计划最多 5 次真实 API 调用，
每次 4 秒 = $0.12/次，**预算上限约 $0.6**。超出预算前必须停下向用户报备，不得静默追加调用。

## 步骤

### 阶段 0：环境自检（不调用 API，免费）

- [x] 确认 `gcloud auth application-default print-access-token` 或用
      `service_account.Credentials.from_service_account_file` 手动构造 token 不报错（用
      `hermes-vertex-key.json`，只验证能否成功换 token，不发送 Vertex 请求）
- [x] 确认 `project-53260abd-312a-4cb0-aa0` 已启用 `aiplatform.googleapis.com`
      （`gcloud services list --enabled --project=... | grep aiplatform`）
- [x] 复制 `.env.example` → `.env`，填入项目 ID 和 key 路径（`.env` 不提交）
- [x] 确认 `hermes-vertex@project-53260abd-312a-4cb0-aa0.iam.gserviceaccount.com` 在该项目上有
      `roles/aiplatform.user`（按 [[hermes-secretary-profile-setup]] 记忆记录应该已有；若阶段 1 报
      403，先查这里，403 本身不计费，代价可接受）

**成功判据**：token 换取成功且 API 已启用。若任一失败，停下向用户报告具体错误，不要猜测绕过。

### 阶段 1：纯文本烟雾测试（验证请求/响应 schema，$0.12）

```bash
uv run video_gen.py "a static shot of a red apple on a wooden table, nothing moves" \
  --model veo-3.1-lite-generate-001 --duration 4 --resolution 720p -o /tmp/smoke-text.mp4
```

- [x] 请求成功提交（拿到 operation name）
- [x] 轮询在合理时间内（<3 分钟）返回 `done: true`
- [x] `/tmp/smoke-text.mp4` 落盘且非空
- [x] `ffprobe /tmp/smoke-text.mp4` 能读出时长/分辨率，确认是合法 mp4

**若失败**：记录真实响应体完整 JSON 结构（脚本已在 `save_videos` 抛错时打印原始 payload 片段），
**逐字段对照 `video_gen.py` 里的所有假设**（不止 `bytesBase64Encoded`/`gcsUri`/`videos`，也包括
`response` 这个顶层 key 本身、`raiMediaFilteredCount`/`raiMediaFilteredReasons` 的结构——这些同样是
未经真实调用验证的推测），逐一修正代码，**修正后必须先补一条单元测试锁住正确的 schema，再继续**。
这一步的重跑不计入下面的预算上限，但仍需在最终汇报里如实说明多花了多少次/多少钱。

### 阶段 2：图生视频测试（真实候选图，$0.12/次，最多 4 次：初始 1 次 + 下方 3 条处理路径各 1 次）

阶段 1（1 次）+ 阶段 2（最多 4 次）= 总预算 5 次 / 约 $0.6，与"成本预算"一节的全局上限对齐。

Prompt 方向（英文，Veo 目前只支持英文 prompt）：手势舞的核心是"上半身/手部的重复节奏动作"，不是走位——
参考 TikTok 常见的 "hand tutting" / "finger dance" 风格描述：

```
prompt = "The woman in the photo stays in place and performs a rhythmic TikTok-style hand gesture
dance to an upbeat pop beat: her hands and fingers move in sharp, syncopated poses in front of her
chest and face, shoulders bounce slightly on the beat, confident expression, looking at the camera.
Vertical video, studio background stays static."
```

```bash
uv run video_gen.py "$PROMPT" \
  --image "/Users/xasxcy/.hermes/profiles/secretary/workspace/mika-base-v1/assets/pro-v2/expression-pack/candidates/DARK-FACE-SHAPE-001.png" \
  --model veo-3.1-lite-generate-001 --duration 4 --resolution 720p --aspect-ratio 9:16 \
  -o /tmp/dance-attempt-1.mp4
```

- [x] 生成成功，`/tmp/dance-attempt-1.mp4` 落盘
- [x] `ffprobe /tmp/dance-attempt-1.mp4` 确认实际宽高比确实是请求的 `9:16`（不能只靠人工看关键帧判断）
- [x] `ffmpeg -i /tmp/dance-attempt-1.mp4 -vf "select='not(mod(n\,16))'" -vsync vfr /tmp/frames/attempt-1-%d.png`
      每 16 帧抽一张（约 5-6 帧，覆盖首/中/尾，比只抽 3 帧更能看出动作节奏），零成本（本地 ffmpeg，不占预算）
- [x] 用 Read 工具依次查看抽出的帧图，人工判断：① 脸是否被裁掉/严重变形 ② 是否有手势舞动作、帧间是否有
      连贯节奏感 ③ 是否明显崩坏（多余肢体、脸部扭曲等生成瑕疵）

**若返回体里 `raiMediaFilteredCount > 0`（被责任 AI 审核过滤，`save_videos` 会直接抛异常中止）**：
这单独算一类失败，不要当成"质量不达标"处理。先检查 `raiMediaFilteredReasons` 的具体内容，然后：
① 确认 `--person-generation allow_adult` 已生效（默认值，脚本已带）；② 把 prompt 里过于写实/暗示身体
接触的措辞去掉，改得更中性（例如强调"舞蹈动作"而非身体部位）；③ 按以上调整后重跑一次，这次重跑算在
下面的 4 次预算内。若调整后仍被过滤，停下向用户报告具体过滤原因，不要连续重试硬闯。

**质量不达标（非 RAI 过滤）的处理路径**（按优先级尝试，每条路径只试一次，别无限重试）：

1. 裁剪切到脸：改用 `--resize-mode pad`，或者用 Pillow 把原图预先 pad 成 9:16 画布（不裁剪只加边）
   后再传入，重跑一次
2. 动作不够"舞蹈感"：调整 prompt 措辞（更具体描述手部动作节奏），重跑一次
3. 明显生成崩坏（AI 常见的多手指/肢体扭曲）：换 `DARK-FACE-SHAPE-001-DRESS-005-fixed-v2.png`（更高分辨率
   输入可能改善细节），重跑一次
4. 以上路径（含 RAI 调整重跑）都试完仍不理想：停下向用户报告效果、贴出关键帧截图，问是否要升级到
   `veo-3.1-fast-generate-001`（更贵但质量更好）或调整方向，不擅自加大预算

### 阶段 3：定稿

- [x] 选定效果最好的一条视频，另存为 `/Users/xasxcy/GitRepositories/video-gen/tests/fixtures/tiktok-gesture-dance-sample.mp4`
      （`.gitignore` 已排除 `tests/fixtures/*.mp4`，不进仓库，只本地留档）
- [x] 把实际用到的 prompt、模型、参数、耗时、花费写进 `tests/README.md` 的 Run log 表格
- [x] 如实测中发现文档/代码描述有误（字段名、region 限制等），同步修正 `SKILL.md` / `README.md`
- [x] 通过 `SendUserFile` 把最终视频发给用户查看

### 阶段 4：收尾

- [ ] 若阶段 1/2 过程中修了 bug，代码修复单独提交（`fix: ...`）
- [ ] 文档更新（README/SKILL.md/tests 记录）单独提交（`docs: ...`）
- [ ] 两次提交都 push 到 `origin/main`
- [ ] 向用户汇报：花了多少钱、最终参数、视频链接/路径

## 明确不做的事（避免过度设计）

- 不做重试/退避机制（README 已注明是已知限制，留到真的需要时再加）
- 不做 GCS 输出路径的实测（当前场景内联字节返回已够用，`--storage-uri` 分支只做到"代码存在、单测覆盖"，
  不烧钱做真实验证）
- 不做 `--last-frame` 首尾帧插值的实测（用户只要求单图生成，这条路径同样只做到单测覆盖）
- 不批量跑多个候选图 / 多组参数做 A/B（成本预算不支持，选一条能用的就停）
