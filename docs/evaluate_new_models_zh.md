# RiskChainBench 新模型评估教程

> 稳定入口：本文件路径固定不变。代码、数据 revision 和运行边界更新时，
> 只更新本文内容，不更换链接。
>
> 最近更新：2026-07-25

## 1. 评估范围

RiskChainBench 分成两个顺序执行、分别计分的任务：

1. **Task 1：混淆消息恢复**
   - 输入：平台侧可见的混淆私信文本。
   - 输出：恢复文本、意图和 Top-k 入口。
   - 主任务只使用每站的 `v000`；`v001` 至 `v005` 仅用于恢复鲁棒性分析。
   - 指标：CER、消息 exact match、入口 exact match、入口 Recall@k 和
     strict success。
2. **Task 2：受控网页调查**
   - 输入：Task 1 已冻结的恢复结果及 600 个本地网页环境。
   - 输出：浏览轨迹、是否违规、违规类型、解释和证据引用。
   - 同时运行 `reference_restoration` 与 `model_restoration`：
     前者隔离网页调查能力，后者测量完整端到端链路。

Task 1 与 Task 2 使用同一个被测模型，但结果分开统计。Task 2 不允许根据网页
观察反向修改 Task 1 的恢复结果。

## 2. 固定数据与代码入口

### GitHub

- 评估总仓库：<https://github.com/mattheliu/riskchainbench-eval>
- Task 1 独立代码：<https://github.com/mattheliu/riskchainbench-task1>
- Task 2 独立代码：<https://github.com/mattheliu/riskchainbench-task2>

### ModelScope

- Task 1：<https://modelscope.cn/datasets/leonliuzx/riskchainbench-task1>
- Task 2：<https://modelscope.cn/datasets/leonliuzx/riskchainbench-task2-controlled-web-replay>

### Hugging Face

- Task 1：<https://huggingface.co/datasets/leonliuzx/riskchainbench-task1>
- Task 2：<https://huggingface.co/datasets/leonliuzx/riskchainbench-task2-controlled-web-replay>

数据仓库只向获授权的研究协作者开放。不得转发 resolver、恢复 gold、原始站点
映射、隐藏动作协议、凭证或未脱敏实验日志。

## 3. Frozen Balanced-600 门禁

正式评估只能使用以下三个合同。Runner 会逐文件校验，并对这三个 SHA-256
执行硬门禁；同名旧数据、重新抽样的 600 条或自洽但被改写的合同都会被拒绝。

| 合同 | SHA-256 |
|---|---|
| Task 1 | `28a2fe6bac429d902153f3d9f2b575b89439115a91e9cd882843d5e54e056240` |
| Task 2 | `028109cd4966e685aa7e611472ef7bc6482a5b83f68f776f9547f7c8175fabed` |
| Task 1 -> Task 2 handoff | `7c2f462ecc92ca57b69675afc4462a348e8c75614bcc5960465c6110fe4fcc4d` |

三者的 `source_benchmark_id` 必须是
`riskchainbench-balanced-600-v0.3`，Task 2 和 handoff 的案例数必须都是
600，handoff 只能使用 Task 1 `v000`。

可移植发布还固定以下 evaluator/runtime 产物：

| 产物 | SHA-256 |
|---|---|
| 600-case handoff map | `78502501b787b892049e45587d1ece84547c960b35f66f4764dfb81f2e336177` |
| Task 2 runtime supplement | `195a60585d726a6feb4dd986ffa324d0fac6a7746f1003ef669f392976689617` |
| Runtime metadata bundle | `60d60bdeb7dd5448e0ea6a8349811075bed3b266a8586698fcb301f13ca14c1c` |

每个数据仓库根目录的 `PORTABLE_OVERLAY_MANIFEST.json` 记录该次发布的
`overlay_sha256`。不要把旧 overlay 哈希写死到运行脚本中；应由下载后的验证器
读取并核验其内嵌值。

## 4. 环境要求

- Linux x86-64；
- Python 3.10；
- Chromium + Playwright；
- Tesseract OCR；
- `zstd`、GNU `tar`；
- 可用磁盘建议至少 25 GB；
- 只使用 `libinfer/libinfer-neo`，禁止 OneAPI；
- `LIBINFER_NEO_URL` 与 `LIBINFER_SK` 写入仅当前用户可读的环境文件。

示例环境文件：

```bash
install -m 700 -d .secrets
install -m 600 /dev/null .secrets/libinfer.env
```

文件内容：

```bash
export LIBINFER_NEO_URL='<libinfer-neo endpoint>'
export LIBINFER_SK='<secret>'
```

不要把凭证写入命令行、Git、模型输入、结果 JSON 或截图。

## 5. 下载数据

建议目录：

```text
riskchainbench-run/
  code/task1/
  code/task2/
  data/task1/
  data/task2/
  runs/preflight/
  runs/task1/
  runs/task2/
  runtime/
```

### 5.1 从 ModelScope 下载

```bash
modelscope login

modelscope download \
  --repo-type dataset \
  leonliuzx/riskchainbench-task1 \
  --local-dir data/task1 \
  --max-workers 8

modelscope download \
  --repo-type dataset \
  leonliuzx/riskchainbench-task2-controlled-web-replay \
  --local-dir data/task2 \
  --max-workers 8
```

### 5.2 从 Hugging Face 下载

```bash
hf auth login

hf download \
  leonliuzx/riskchainbench-task1 \
  --repo-type dataset \
  --local-dir data/task1 \
  --max-workers 8

hf download \
  leonliuzx/riskchainbench-task2-controlled-web-replay \
  --repo-type dataset \
  --local-dir data/task2 \
  --max-workers 8
```

国内网络需要代理时，大写和小写变量都设置：

```bash
export http_proxy=http://agent.baidu.com:8891
export https_proxy=http://agent.baidu.com:8891
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"
```

不要同时从两个平台拼接一套运行目录。可用一个平台作为主下载源，另一个平台只
用于 SHA-256 回读或断点恢复。

### 5.3 下载后完整性检查

ModelScope 与 Hugging Face 提供相同的 portable overlay。下载完成后分别执行：

```bash
python data/task1/tools/verify_split_task_platform_overlay.py \
  --root data/task1

python data/task2/tools/verify_split_task_platform_overlay.py \
  --root data/task2
```

两次都必须返回 `status=PASS`，且 `case_count=600`。验证器会检查 overlay
内嵌哈希、逐文件 SHA-256、handoff map，以及 Task 2 runtime supplement/bundle。
平台仓库可能包含既有的 Docker 基础文件；这些额外文件不影响 overlay 校验。

## 6. 获取代码与安装依赖

```bash
git clone https://github.com/mattheliu/riskchainbench-task1.git code/task1
git clone https://github.com/mattheliu/riskchainbench-task2.git code/task2

python3.10 -m venv .venv
source .venv/bin/activate
pip install -r code/task1/requirements-task1.txt
pip install -r code/task2/requirements-task2.txt
python -m playwright install chromium
```

## 7. 路由预检

每个待评模型必须先通过两次本地生成图片的读取探针。探针只证明该
`libinfer-neo` 路由支持图像输入，不代表模型任务得分。

```bash
MODELS='gpt-5.4,claude-opus-4-8-kiro,kimi-k2.6,gemini-3.6-flash'

python code/task2/scripts/probe_libinfer_multimodal_routes.py \
  --env-file .secrets/libinfer.env \
  --models "$MODELS" \
  --priority "$MODELS" \
  --probe-count 2 \
  --out runs/preflight/all_routes.json
```

只有 `status=PASS_FIXED_MLLM_SELECTED` 且具体模型为
`PASS_MULTIMODAL_ROUTE` 才能继续。路由探针中的模型 ID 必须与 Task 1 /
Task 2 的 `--models` 完全一致。

## 8. 还原 Task 2 本地运行环境

Task 2 数据包保存的是 600 个经过 SHA-256 固定的 OCI/Docker 归档。不要依赖
原机器的绝对路径，也不要求逐个执行 `docker load`。Materializer 会：

1. 校验 Task 2 和 runtime supplement；
2. 校验 600 个 archive manifest；
3. 按需解压 OCI layer；
4. 校验 `mirrorserve` 二进制 hash；
5. 将 L2 observation plan 和 L3 stateful profile 放入同一 runtime root。

先做单案例 smoke：

```bash
python code/task2/scripts/materialize_task2_runtime.py \
  --task2-release data/task2 \
  --runtime-root runtime \
  --case-ref CASEd150dbe314a6c11bc1fa \
  --workers 1 \
  --report runs/preflight/materialize-smoke.json
```

通过后再还原 600 条：

```bash
python code/task2/scripts/materialize_task2_runtime.py \
  --task2-release data/task2 \
  --runtime-root runtime \
  --workers 4 \
  --report runs/preflight/materialize-600.json
```

重复运行默认会复核并跳过已正确还原的站点。只有明确需要重建损坏目录时才使用
`--replace`。不要手动修改 materialized site、profile 或 observation plan。

## 9. 运行 Task 1

建议先只跑主任务 `v000`，得到 Task 2 所需的冻结输入：

```bash
python code/task1/scripts/run_task1_four_model_matrix.py \
  --release data/task1 \
  --route-probe runs/preflight/all_routes.json \
  --out runs/task1 \
  --models "$MODELS" \
  --tracks primary \
  --model-workers 4 \
  --task-concurrency 3 \
  --system-retry-rounds 2
```

主结果完成后，可单独跑五个恢复鲁棒性变体：

```bash
python code/task1/scripts/run_task1_four_model_matrix.py \
  --release data/task1 \
  --route-probe runs/preflight/all_routes.json \
  --out runs/task1-robustness \
  --models "$MODELS" \
  --tracks robustness \
  --model-workers 4 \
  --task-concurrency 3 \
  --system-retry-rounds 2
```

Task 1 的系统错误允许有限重试；模型拒答、无法解析或不符合输出 Schema 必须保留
为模型失败，不能使用 gold 自动修复。

## 10. 运行 Task 2

Task 2 会先等待每个模型的 Task 1 主任务完成，然后分别跑 reference restoration
和 model restoration：

```bash
python code/task2/scripts/run_task2_four_model_matrix.py \
  --task1-release data/task1 \
  --task2-release data/task2 \
  --handoff-release data/task2 \
  --task1-results runs/task1 \
  --route-probe runs/preflight/all_routes.json \
  --runtime-root runtime \
  --out runs/task2 \
  --models "$MODELS" \
  --model-workers 2 \
  --task-workers 2 \
  --system-retry-rounds 2 \
  --wait-for-task1-seconds 86400 \
  --wait-poll-seconds 30
```

固定运行参数：

- `--max-steps 12`
- `--max-tokens 4096`
- `--max-judge-images 8`
- `--max-judge-image-bytes 5500000`
- viewport-only 截图

未经统一实验协议变更，不要单独为某个模型放宽这些参数。

## 11. 查看进度

```bash
python code/task2/scripts/report_split_four_model_progress.py \
  --task1-root runs/task1 \
  --task2-root runs/task2
```

机器可读版本：

```bash
python code/task2/scripts/report_split_four_model_progress.py \
  --task1-root runs/task1 \
  --task2-root runs/task2 \
  --json
```

关键产物：

```text
runs/task1/models/<model>/primary/run/summary.json
runs/task1/models/<model>/primary/run/predictions.jsonl
runs/task2/models/<model>/handoff/handoff_manifest.json
runs/task2/models/<model>/reference_restoration/condition_report.json
runs/task2/models/<model>/model_restoration/condition_report.json
runs/task2/models/<model>/*/base/cases/<CASE>/case_result.json
runs/task2/models/<model>/*/base/cases/<CASE>/model_calls.json
```

## 12. 结果解释

### Task 1

- `valid_prediction_count + failed_prediction_count` 必须等于目标题数；
- `v000` 的 600 条用于主结果和 Task 2 handoff；
- `v001` 至 `v005` 只用于 Task 1 鲁棒性分析；
- 入口恢复错误不会被改正；对应案例在 model-restoration 条件下不可调查。

### Task 2

- `PASS`：系统执行完整且产生可评估轨迹；
- `MODEL_FAILURE`：路由可用，但模型拒答、格式错误、无效动作或未完成协议；
- `CASE_SYSTEM_FAILURE`：浏览器、运行时、文件、传输等系统问题；
- 只有 `CASE_SYSTEM_FAILURE` 进入有限系统重试；
- 网页不可达、页面缺失、动作预算耗尽和证据不足必须分别记录，不能都改写为
  `NON_VIOLATION`。

### 正式证据评分边界

当前 Task 2 runner 会生成模型自身的最终判断和证据引用。论文正式的外部
`J_phi` 应是**独立固定的多模态 Judge**，只根据实际轨迹输出：

1. evidence faithfulness；
2. evidence sufficiency；
3. investigation completeness；
4. reasoning consistency。

旧的 Rule/Text/VLM 加权综合分和 token-overlap 分只可作为
`diagnostic/ablation`，不能作为论文主表的正式证据链得分。正式 Judge 发布前
还必须完成固定模型/Prompt/采样参数、人工一致性、Jensen-Shannon 散度和受控
轨迹扰动验证。

网站准确率和违规类型 F1 需要独立 Human Gold。Human Gold 尚未冻结时，只能
报告运行覆盖、协议有效性、模型输出分布和诊断性证据审计，不能声称正式
accuracy/F1。

## 13. 常见错误与边界条件

1. **误用旧的 600 条数据**
   - 症状：合同身份、SHA 或 handoff 不一致。
   - 处理：重新下载正式 `Balanced-600 v0.3`，不要修改合同后重算 SHA。
2. **只下载 Hugging Face/ModelScope 的模型可见文件**
   - 症状：缺少 `evaluator_only` 或 `runtime_supplement`。
   - 处理：确认账号已获得 evaluator 访问权限，并下载完整私有快照。
3. **把 Task 1 六个变体都送入 Task 2**
   - 正式 handoff 只接受 `v000`；其余五个变体仅做恢复鲁棒性。
4. **Task 1 与 Task 2 模型 ID 不一致**
   - handoff fail-closed；不能把一个模型的恢复结果交给另一个模型冒充端到端。
5. **入口恢复失败后使用 reference entry 补齐**
   - model-restoration 禁止自动纠正。reference-restoration 必须单列报告。
6. **OneAPI 或非固定路由混入**
   - Runner 会拒绝包含 `oneapi` 的 endpoint；每个模型必须先过当前路由探针。
7. **上游 502、超时与模型失败混淆**
   - 502/网络超时属于系统失败，可有限重试；拒答、坏 JSON、无效动作属于模型
     失败，不得无限重试到成功。
8. **WordPress `wp-skip-link` 被当成可点击主控件**
   - 新 runner 会过滤 `screen-reader-text`、`sr-only`、`visually-hidden`、
     `aria-hidden`、`inert` 及微小 clipped 控件；旧 runner 可能等待 30 秒超时。
9. **全页截图超过视觉模型限制**
   - 正式矩阵使用 viewport-only，并固定 8 张图、5.5 MB 总图片预算。
10. **L3 表单只点提交，不先填写**
    - L3 stateful profile 要求按页面可见控件完成 `fill/check -> submit`；
      只点导航或重复点击会被记为模型动作失败。
11. **L2 被误认为不可做 Web Work**
    - L2 可以执行安全的本地可见点击并观察状态变化，只是没有 L3 的完整持久化
      profile；两类必须分层报告。
12. **外链或真实支付被执行**
    - 浏览器仅允许当前本地 runtime、`data:`、`blob:`、`about:`。外网、真实支付、
      真实账号和第三方通信一律阻断并记录。
13. **截图、OCR、文字描述被算成三份独立证据**
    - 同一页面状态的像素、OCR 和描述属于同一 dependency group，不能重复计分。
14. **把页面打不开当作正常网站**
    - 不可达属于环境限制或系统结果，不是 `NON_VIOLATION` 的充分证据。
15. **并发过高**
    - 建议同时最多 4 个模型；Task 2 每模型先用 2 workers。升并发前观察
      libinfer QPS、OCR CPU、文件句柄和内存，不要因限流改变不同模型的重试预算。
16. **结果目录复用**
    - 每次实验使用新目录；Runner 的 lock 和 config fingerprint 用于防止两个批次
      写入同一输出。
17. **私有信息泄漏**
    - 不得将 resolver、source stratum、seed 标签、真实域名信誉、hidden selectors、
      fixture 值或 peer answer 放入模型输入。
18. **修改 Prompt 后继续沿用旧结果**
    - Prompt、Schema、codebook、模型路由或动作预算变化都必须生成新 run ID，
      不能与旧结果直接合并。

## 14. 外发前验收

- 三个冻结合同 SHA 全部匹配；
- 路由探针显示 `libinfer-neo` 且 `oneapi_used=false`；
- runtime supplement 和目标 OCI archive 全部通过 hash；
- smoke case 能启动本地 runtime，且零外网请求；
- Task 1 目标题数全部 accounted；
- Task 2 reference/predicted 两列分开；
- 系统失败、模型失败、弃答和不可调查没有混算；
- 结果中没有凭证、原始 resolver、真实入口或未脱敏截图；
- 未完成 Human Gold/Judge 校准时，没有发布 accuracy/F1 或正式证据链总分。
