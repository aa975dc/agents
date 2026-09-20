# FIX-06 测试计数口径（unittest discover）

日期：2026-09-21。对象：本仓库 `optimization-v1` @ 2656364 + FIX-06 未提交改动（tests/closure_r05、tests/packaging、tests/indexing/test_scan.py、tools/build_vendor.py archive 模式）。
对应复核报告 §2（SR-07）：复核者实测 682 collected / 648 testsRun / 645 success / 2 failure / 1 方法 skip / 34 类级门控（Linux x86_64，Python 3.13.5，候选 1a7bc698）。

## 1. 口径定义（本机以最小用例实测过 unittest 3.9 语义）

| 项 | 定义 |
|---|---|
| collected | `TestLoader.discover("tests")` 展开后的全部测试方法数，**含类级门控类的方法** |
| testsRun | `result.testsRun`：实际进入 runner 的测试数（含方法级 skip）。类级门控（`setUpClass` 抛 `unittest.SkipTest`）的每个类只产生 **1 条 skip 记录（ErrorHolder）**，其成员方法**不进入 testsRun**（本机用最小用例实测：2 方法门控类 → testsRun 0、skipped 1 条） |
| success | testsRun − failure − error − 方法级 skip 数 |
| failure / error | runner 结果分列，不与 skip 混算 |
| 方法级 skip | 装饰器/`skipTest()` 产生、绑定在具体测试方法上的 skip 记录 |
| 类级 skip（门控） | `setUpClass` SkipTest 产生的 ErrorHolder 记录，一条对应一个门控类 |

**基准门控不算成功**：L/XL/XXL 档基准（tests/benchmarks/test_l_tier、test_xl_tier、test_xxl_tier）由 `L_TIER=1` 环境门控制，未设门时 12 个门控类、34 个测试方法完全不执行——既不进入 testsRun，也不计入 success，只留 NOT_RUN 门控记录。任何"725 全过"的表述不含这 34 个基准用例。

## 2. 本机实测（macOS，同口径）

环境：macOS（darwin arm64）、系统 Python 3.9.6、unittest 3.9 语义。复现命令：

```bash
python3 - <<'EOF'
# discover 展开计 collected；SpyResult 记录每条 skip 归类（类级/方法级）后真实运行
import io, unittest
loader = unittest.TestLoader()
suite = loader.discover("tests")
# ... flat 展开计 collected，TextTestRunner(resultclass=SpyResult) 真实运行 ...
EOF
python3 -m unittest discover -s tests -q   # 标准口径：Ran 725 tests ... OK (skipped=12)
```

实测结果（2026-09-21，FIX-06 改动后）：

| 项 | 数值 |
|---|---|
| collected | **759** |
| testsRun | **725** |
| success | **725**（failure 0，error 0） |
| 方法级 skip | **0**（macOS 上无平台条件跳过命中） |
| 类级门控 | **12 条记录 / 34 个方法不运行**（12 个门控类清单见下） |
| Node | 65/65 PASS（`node --test tests/swarm_workflow.test.mjs tests/host/host-contract.test.mjs`） |

12 个门控类：benchmarks.test_l_tier TestStep1–5（5）、test_xl_tier TestStep1–4（4）、test_xxl_tier TestStep1–3（3），reason 均为 `NOT_RUN：未设置 L_TIER=1 …`。

## 3. 与复核者数字的对照（不互改身份）

| 项 | 复核者（Linux，候选 1a7bc698） | 本机（macOS，HEAD 2656364+FIX-06） | 差异解释 |
|---|---|---|---|
| collected | 682 | 759 | FIX-01…05 批新增 +73；FIX-06 本轮 +4（R05 darwin 条件用例 1、archive vendor 用例 3） |
| testsRun | 648 | 725 | 同上；两类口径下 34 个基准门控方法均不进入 |
| success | 645 | 725 | 复核者 2 个 failure 均为 SR-07 测试缺陷，本轮修复（见下） |
| failure | 2 | 0 | ① R05 `/private/var` 断言（macOS 特例）→ 拆为可控临时 symlink 用例 + darwin 条件用例；② packaging commit 非空断言（无 .git 失败）→ build_vendor archive 模式 + 测试分组 |
| error | 0 | 0 | — |
| 方法级 skip | 1 | 0 | 复核者环境命中 1 条方法级跳过（其环境记录，不改写）；本机新增的 darwin 条件用例在 Linux/CI 将成为 1 条方法级 skip（注明原因），两个 failure 消失 |
| 类级门控 | 12 类 / 34 方法 | 12 类 / 34 方法 | 门控机制一致，数字同构 |

历史证据身份保留：复核者 Linux 数字与其环境绑定，本文不覆盖、不改写；上表仅做同口径对照。

## 4. Git checkout 与 archive 证据口径分列（两种形态实测记录）

同一 `tools/build_vendor.py` 入口，两种形态分别真实运行 packaging 测试：

- **Git checkout 形态**（本仓库，含 .git）：`python3 -m unittest discover -s tests -p "test_vendor.py" -v` → **Ran 9 tests，OK**（VendorBuildTests 6 项断言真实 HEAD SHA、MANIFEST 无 provenance；ArchiveFormVendorTests 3 项在临时去 .git 副本上复核 archive 口径）。
- **模拟 archive 形态**（tempfile 复制 marketplace.json / packages/agents_kernel / dev-companion / code-analysis-swarm / tools+tests 子集，**无 .git**）：同命令 → **Ran 3 tests，OK (skipped=1)**：VendorBuildTests 整组类级 skip 并注明"archive 形态：Git checkout 断言不适用"；ArchiveFormVendorTests 3 项全过（`archive:<标记>` + `provenance=archive_no_git`、逐文件 sha256 仍与源一致、缺省 `archive:none`、SHA 形态声明被拒绝 exit 2）。
- 附：tests/indexing/test_scan.py 在无 .git 副本上 10/10 全过——git 扫描用例按"系统 git 可用性"判定，装 git 即真实执行，不再依赖候选是否 .git。

禁止事项（已用测试固化）：不得 `git init` 假仓库冒充候选 SHA；无 .git 时 build_vendor 拒绝一切 SHA 形态的 `AGENTS_SOURCE_COMMIT`，只接受 `archive:` 前缀的诚实标记。
