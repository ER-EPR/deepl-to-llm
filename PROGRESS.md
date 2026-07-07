# deepl-to-llm 进展总结（切到 Collabora 项目继续）

## 一句话结论

**带页眉页脚的文档翻译时，页眉/页脚内容被 Collabora 粘贴进每个表格单元格——这是 Collabora 侧的 bug，与 backend（bridge/LLM/DeepL）完全无关。已用真 DeepL 裸透传证实。修复必须改 Collabora 的 `translatehelper.cxx`。**

---

## 证据链（决定性）

最后一步 A/B 测试彻底排除了 backend：

1. **bridge 零介入的裸透传**：当 `DEEPL_API_KEY` 设置且 `tag_handling=html` 时，bridge 把 Collabora 发来的原始 HTML 字符串**原封不动**转发给真 DeepL，DeepL 返回什么就**原样**回给 Collabora。不做 `extract_text_nodes`、不做 `refill_text_nodes`、不碰 lxml。日志标记 `[BACKEND] DeepL(raw) served`。

2. **真 DeepL 也复现**：用真 DeepL free key 跑同一个文档（选中表格前两列翻译），结果**和用 LLM 时一样**——header 翻译被塞进每个 cell，页码 Page 1→2→3 递增，中英文混杂。

3. **Collabora 只发了 1 个请求**：`docker logs` 显示整个翻译过程只发了 **1 个请求**（header div），**没有任何 cell 请求**。但最终结果里每个 cell 都带上了 header 翻译。说明 Collabora 收到 header 的翻译后，自己把它粘贴到了选区里每个 cell 的游标位置。

```
docker logs 统计：
  [REQ] tag_handling 出现次数 = 1   ← 只有 header 一个请求
  [REQ] text[0] 内容 = <div title="header">...   ← 是 header，不是 cell
```

**结论**：backend 收到 header 请求、返回正确翻译，错的是 Collabora 往哪贴。

---

## Collabora bug 的机制

来自对 `CollaboraOnline/online.mirror` 源码的调研：

- **调用链**：用户点"翻译" → `translatelangselect.cxx`（对话框）→ `translatehelper.cxx::TranslateDocumentCancellable()`（循环）→ `linguistic::Translate()`（libcurl HTTP）。
- **节点迭代**：`translatehelper.cxx` 的 `for (SwNodeOffset n(startNode); n <= endNode; ++n)` 按**节点数组顺序**遍历，每个文本节点：`ExportPaMToHTML(cursor)` → `linguistic::Translate()` → `PasteHTMLToPaM(cursor, translatedHTML)`。
- **header/footer 在节点数组里位于正文之前**（"Extras" section，索引在 body 之前）。选区跨页时，header/footer 节点被纳入循环范围。
- **paste 错位**：header 翻译完，`PasteHTMLToPaM` 把 header 的 HTML 块贴到了选区里每个 cell 的游标，而不是只贴回 header 自己的位置。每翻一个 cell，header 又被重新翻译（页码 `<sdfield PAGE>` 随页递增），所以看到 Page 1→2→3 + header 堆叠。
- **超时**：`translate.cxx:25` 的 `constexpr tools::Long CURL_TIMEOUT = 10L;` 是**硬编码**的 `CURLOPT_TIMEOUT`（硬总超时，非空闲超时），无配置项、无环境变量、无 Helm value。响应全缓冲、同步阻塞。

---

## 待改的 Collabora 文件（3 个，跨 2 个库）

| 文件 | 库 | 改动 |
|---|---|---|
| `engine/include/linguistic/translate.hxx` | `liblng.so`（小） | 签名：`OString Translate(...)` → `std::vector<OString> Translate(..., const std::vector<OString>&)` |
| `engine/linguistic/source/translate.cxx` | `liblng.so`（小） | POST body 循环 `&text=<a>&text=<b>...`；响应遍历整个 `translations` 数组；`CURL_TIMEOUT` 可调大 |
| `engine/sw/source/uibase/shells/translatehelper.cxx` | `libsw.so`（巨大） | 循环改成 collect-then-batch；**或更小的补丁：循环时跳过 header/footer 节点** |

- `linguistic::Translate` **全局只有一个调用者**：`translatehelper.cxx`。改签名是封闭的。
- `?tag_handling=html` 是 `translatehelper.cxx` 里 `IsTranslationServiceConfigured()` 拼到 URL 上的，不在 `translate.cxx`。
- DeepL text API 支持 `text` 数组（最多 50 条），返回等长同序 `translations`——批量可行。
- DeepL text API 是**同步**的；Document API 是整文件异步，不适合段落级。

---

## 建议的 Collabora 修复路径（按工作量从小到大）

### 方案 A（最小补丁，先试）：循环跳过 header/footer 节点

在 `translatehelper.cxx` 的 `for` 循环里，判断当前节点是否在 header/footer section，是则 `continue` 跳过（不翻译、不 paste）。这样 header 不进 cell paste 循环。可能十几行。需要查 Writer 节点模型里判断 header/footer 的 API（`SwHeaderStartNode`/`SwFooterStartNode` section 类型，或节点所属 section）。

**风险**：header/footer 自己就不翻译了（如果用户需要翻译页眉页脚，这个方案牺牲它）。但能止住 cell 被污染。

### 方案 B（治本）：collect-then-batch 改造

`for` 循环改成：收集多个文本节点（各带 cursor+HTML）→ 一次 `linguistic::Translate`（数组）→ 按序 `PasteHTMLToPaM` 回填。约 55 行，跨 3 文件。改 paste 模型，应能消除 header 窜入 cell。`linguistic::Translate` 签名要改成接受 `vector`。

### 部署约束

用户用**官方 Collabora Helm chart**（`collaboraonline.github.io/online`），不想维护完整 fork。所以：
- `liblng.so`（小，18 源文件）可单独重编替换。
- `libsw.so`（巨大，含 `translatehelper.cxx`）重编重、ABI 风险高——方案 A/B 都要碰它。
- `LD_PRELOAD` 拦截 libcurl 只能改超时，**改不了 paste 逻辑**——对这个 bug 无用。
- 现实方案：多阶段 Dockerfile，builder 阶段打补丁编译 `libsw.so`，runtime 拷贝进官方镜像。锁定版本号重编保 ABI。

---

## deepl-to-llm 项目当前状态（已完成的 backend 工作）

**8 个 commit 在本地 `main`**（未 push）：

```
496d3c1 Add raw DeepL passthrough for HTML (bypass extract/refill)
372a844 Add DeepL passthrough with LLM fallback for A/B testing + quota resilience
251ed84 Add full diagnostic logging of client request, LLM call, and response
2cb463d Use thinkingBudget=512, not 0 — prevents empty-span fragment dropping
e7db62c Disable Gemini thinking + add high-threshold batching safety net
0910846 Use modern ENV key=value format in Dockerfile
b65cb2f Replace hardcoded secrets with placeholders
477f853 Preserve markup in LLM translation; add tests and .gitignore
```

### backend 能力（都已验证）

- **结构保留翻译**（LLM 通路）：lxml 解析 HTML → 抽文本节点 → JSON `{"items":[...]}` 结构化输出 → 1:1 校验 → 回填。标签/属性/页码原样保留。
- **DeepL 裸透传**（HTML 通路）：`DEEPL_API_KEY` 设置时，原始 HTML 直接转发 DeepL，返回原样回给 Collabora。`[BACKEND] DeepL(raw) served`。
- **DeepL 优先 + LLM 兜底**：DeepL 429/456/网络错误时回退 LLM。
- **thinking=512**：gemini-2.5-flash 最小有效思考预算，避免短片段被吞成空 `<span></span>`（这是当初重复页眉页脚的共犯——thinking=0 会丢 `' 页'` 这种低上下文片段）。
- **高阈值 batching 安全网**：max 500 items / 12000 chars，正常段落不拆（保上下文），只防病态大 payload。
- **诊断日志**：`LOG_LEVEL=debug` 记录 `[REQ]/[HTML]/[LLM-REQ]/[LLM-RESP]/[DEEPL-RAW-REQ]/[DEEPL-RAW-RESP]/[BACKEND]/[RESP]` 全链路。
- **46 个单元测试全过**。

### 配置（docker-compose env）

```yaml
environment:
  - LLM_API_URL=https://example.com/v1/chat/completions
  - LLM_API_KEY=<your LLM key>
  - LLM_MODEL=gemini-2.5-flash
  - BRIDGE_TOKEN=<设置，否则鉴权关闭>
  - DEEPL_API_KEY=<DeepL free key，:fx 结尾>   # 可选，设了就 DeepL 优先
  - LOG_LEVEL=debug                            # 可选
```

`.env`（含 `DEEPL_API_KEY`）已 gitignore，未追踪。

### backend 侧已排除的疑似根因

- ~~LLM 输出 JSON 不可靠~~ → 用 JSON 结构化输出 + 1:1 校验，且真 DeepL 也复现，排除。
- ~~bridge 的 extract/refill 破坏结构~~ → 裸透传（零处理）也复现，排除。
- ~~thinking=0 丢片段~~ → 已改 512，且真 DeepL 不丢片段也复现，排除。
- ~~10s 超时~~ → DeepL 1.69s 返回，远低于 10s，排除。
- ~~RPM 限流~~ → 单次只 1 个请求，排除。

**唯一剩余变量：Collabora 的 paste 逻辑。**

---

## 切到 Collabora 项目后的第一步

1. **先做零代码验证**：用 DeepL backend 跑**整篇翻译**（非选区）同一文档，看 header 是否还窜进 cell。
   - 整篇也窜 → 必须改 Collabora（方案 A 或 B）。
   - 整篇干净 → 只是选区翻译特有，范围更小。

2. **查 `translatehelper.cxx` 节点判断**：找 Writer 里判断"当前节点是否 header/footer section"的 API，为方案 A（循环跳过 header/footer）写补丁。

3. **评估编译部署**：多阶段 Dockerfile 重编 `libsw.so`，锁定 Collabora 版本号保 ABI，Helm 用自定义 image。

---

## 关键文件路径速查

- deepl-to-llm 仓库：`/home/eli/deepl-to-llm`
  - `main.py` — endpoint + LLM/DeepL 路由 + 裸透传
  - `bridge.py` — 结构保留翻译核心（extract/refill/batch）
  - `test_bridge.py` / `test_endpoint.py` — 46 个测试
  - `smoke_live.py` — live LLM 冒烟测试
  - `.env` — DEEPL_API_KEY（gitignored）
- Collabora 源码（`CollaboraOnline/online.mirror`）：
  - `engine/sw/source/uibase/shells/translatehelper.cxx` — **要改的循环**
  - `engine/linguistic/source/translate.cxx` — libcurl 调用 + 10s 超时
  - `engine/include/linguistic/translate.hxx` — Translate 签名
  - `engine/sw/source/ui/misc/translatelangselect.cxx` — 翻译对话框
  - `engine/cui/source/options/optdeepl.cxx` — DeepL 配置 UI（只有 URL + AuthKey）
