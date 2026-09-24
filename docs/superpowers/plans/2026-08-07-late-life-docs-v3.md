# 晚年文书包 v3 具体条款版实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 基于现有自书遗嘱/非公证继承手册、北京生前预嘱公开材料、同行健康与财产材料以及借款协议/维权手册，制作 6 份可填写、可签署、可归档的具体条款型 Word 文书包。

**Architecture:** 每份文书包均采用“使用说明与适用边界 → 事实梳理表 → 具体条款模板 → 签署/见证/交付记录 → 自查与保存”的固定结构。模板中的空白只用于真实事实、金额、日期、姓名、证件号和选择项；权利义务、办理流程、违约、撤销、报告、证据和争议处理均写成可选择或可直接修改的条款。

**Tech Stack:** Bundled Python 3、python-docx、现有 `render_docx.py`、pdfplumber/pypdf；Word 输出放入 `deliverables/晚年IP文书包-v3/`，不覆盖 v1/v2。

## Global Constraints

- 延续参考文档：A4 页面、上下 1 英寸、左右 1.25 英寸、正文约 10.5 磅、Heading 1/2/3 层级、段落留白和表格留白。
- 每个文件必须包含具体条款，不得用“具体约定：________”作为唯一内容。
- 每个文件必须有“适用场景、不能解决的问题、填写说明、签署/保存/更新说明”。
- 生前预嘱采用北京公开《我的五个愿望》的五部分结构作为参考，并标明不冒充协会官方文件、不保证医疗机构必然执行。
- 借款文书沿用现有《借款协议》的条款顺序，并吸收《民间借贷维权办理手册》的合意、交付、还款、催款和证据链要求。
- 所有待填内容使用下划线或勾选项；提示内容不得混入正式签署正文。
- 旧版 `deliverables/晚年IP文书包/` 与 `deliverables/晚年IP文书包-v2/` 保留不动。
- 生成后必须逐份执行 DOCX 渲染，检查每一页的表格、分页、签署区、下划线和页脚。

---

### Task 1: 固化来源结构与 v3 文档目录

**Files:**
- Create: `docs/superpowers/specs/2026-08-07-late-life-docs-v3-design.md`
- Read: `/Users/shaoxinjiang/Documents/遗嘱业务相关材料/自书遗嘱20260715 版本.docx`
- Read: `/Users/shaoxinjiang/Desktop/业务资料/非公证继承课程/非公证继承办理手册完整版.docx`
- Read: `/Users/shaoxinjiang/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/shaoxinjiang_8d44/msg/file/2026-07/生前预嘱文件.pdf`
- Read: `/Users/shaoxinjiang/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/shaoxinjiang_8d44/msg/file/2026-07/个人健康说明书.pdf`
- Read: `/Users/shaoxinjiang/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/shaoxinjiang_8d44/msg/file/2026-07/个人生活照护意愿书.docx`
- Read: `/Users/shaoxinjiang/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/shaoxinjiang_8d44/msg/file/2026-07/财产情况梳理说明书.pdf`
- Read: `/Users/shaoxinjiang/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/shaoxinjiang_8d44/msg/file/2026-07/医疗备用金说明书.pdf`
- Read: `/Users/shaoxinjiang/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/shaoxinjiang_8d44/msg/file/2026-07/我的晚年支持地图.pdf`
- Read: `/Users/shaoxinjiang/Desktop/业务资料/福利品借款协议/借款协议.docx`
- Read: `/Users/shaoxinjiang/Desktop/维权手册产品/民间借贷维权办理手册.docx`

**Steps:**

- [ ] 记录每个来源的具体章节、条款和表单字段，避免只依赖抽象总结。
- [ ] 将北京公开材料的五个愿望、两名见证人声明、修改重签、原件保存和医疗档案留存纳入健康包目录。
- [ ] 将同行健康说明书的基础健康、慢性病、用药、过敏、医院、照护和联系人字段纳入健康包。
- [ ] 将同行财产说明书的房产、银行、定期、保险、证券、证件、债务和联系人字段纳入财产/授权/再婚包。
- [ ] 将借款协议的第 1-11 条及维权手册的催款、录音、电子证据、证据目录纳入借款包。
- [ ] 将目录和条款映射写入 design spec，并做占位符扫描，确保没有“以后补充”类内容。

**Verification:**

```bash
rg -n "五个愿望|见证人|慢性病|用药|过敏|房产|定期存款|借款用途|实际交付|送达地址|证据目录" docs/superpowers/specs/2026-08-07-late-life-docs-v3-design.md
```

### Task 2: 建立参考手册版式与具体条款构建器

**Files:**
- Create: `scripts/generate_late_life_packages_v3.py`
- Reuse: `scripts/generate_late_life_packages_v2.py` only for safe formatting helpers; do not copy its empty-content sections as final content.

**Steps:**

- [ ] 设置 A4、页边距、页脚、Normal、Heading 1/2/3、List Bullet/List Number 样式。
- [ ] 实现固定 DXA 表格宽度、列宽、单元格内边距、表头底色、跨页重复表头。
- [ ] 实现 `article()` 函数，用“第一条/第二条”输出带实际权利义务的条款。
- [ ] 实现 `choice_clause()` 函数，用“□适用/□不适用/□方案一/□方案二”承载可选择分支。
- [ ] 实现 `signature_page()`、`witness_statement()`、`handover_log()`、`self_checklist()` 等公共组件。
- [ ] 统一把填写提示放在正文前或正文后，不把“注意：”混入正式合同条款。

**Verification:**

```bash
/Users/shaoxinjiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m py_compile scripts/generate_late_life_packages_v3.py
```

### Task 3: 生成健康照护与意定监护具体条款包

**Files:**
- Output: `deliverables/晚年IP文书包-v3/01-生前预嘱与健康照护包-v3.docx`
- Output: `deliverables/晚年IP文书包-v3/02-意定监护模板及指导手册-v3.docx`

**Steps:**

- [ ] 健康包加入《个人健康说明书》字段：基础信息、日常活动能力、三项慢性病、三项常用药、备用急救药、药物/食物/其他过敏、主治医生、重大病史、日常照护、两名联系人、更新记录。
- [ ] 健康包加入《个人生活照护意愿书》具体选项：养老地点排序、房间环境、饮食处理、作息、卫生、活动、探视、宗教/精神需求、护工语言与性格。
- [ ] 健康包按照五个愿望分别写入医疗服务、生命支持治疗、他人如何对待本人、家人朋友知悉事项、协助决策人的勾选条款和补充说明。
- [ ] 健康包写入两名见证人声明：已充分讨论、本人神志清楚、无胁迫欺骗、在场签署、签名和日期；另加修改、撤回、保存、复印件交医疗档案的条款。
- [ ] 监护包加入“支持地图”和五个候选人评估问题，形成候选人选择依据。
- [ ] 监护协议写明触发条件确认、医疗决定权限、照护/居住权限、日常财产权限、重大处分权限、禁止事项、监督人、报告周期、账目、费用、利益冲突、替补和终止。
- [ ] 监护包加入监护人接受声明、监督人确认、证件附件清单和生效/办理记录。

**Verification:**

```bash
/Users/shaoxinjiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/generate_late_life_packages_v3.py
```

### Task 4: 生成银行授权与财产赠与具体条款包

**Files:**
- Output: `deliverables/晚年IP文书包-v3/03-银行事务授权模板及手册-v3.docx`
- Output: `deliverables/晚年IP文书包-v3/04-房产车辆财产赠与协议模板手册-v3.docx`

**Steps:**

- [ ] 银行包写明医疗备用金账户、启用条件、使用人、用途限制、单笔/累计额度、双人确认、票据保存和月度报告。
- [ ] 银行授权按“账户查询、缴费、取款、转账、挂失/补卡、其他”逐项勾选，并写明授权期限、撤销、不得转授权、不得使用密码、银行受理记录。
- [ ] 银行包加入资金使用记录、交接清单、撤销通知书和银行办理失败记录，明确失败不等于授权已经生效。
- [ ] 赠与包按房产、车辆、其他财产分开写权属、共有份额、现状、抵押查封、赠与比例、接受、附义务、居住使用、交付、登记、税费、维修、风险转移、违约、返还和争议条款。
- [ ] 赠与包加入房产/车辆交付验收单、权证/钥匙/车辆附件清单、过户受理记录和家庭沟通确认。

### Task 5: 生成借款与再婚财产具体条款包

**Files:**
- Output: `deliverables/晚年IP文书包-v3/05-借条与还款确认模板手册-v3.docx`
- Output: `deliverables/晚年IP文书包-v3/06-再婚与婚前财产包-v3.docx`

**Steps:**

- [ ] 借款包完整输出借款协议：用途、金额、交付、期限、利率、利息支付、还款账户、抵充顺序、逾期、提前到期、实现债权费用、担保、送达、争议解决、生效和份数。
- [ ] 借款包输出借条、现金收据、分期还款表、还款确认书、欠款事实确认单、微信/短信催款模板、通话录音提纲、电子证据固定清单、证据目录。
- [ ] 再婚包把婚前财产清单扩充为房产、车辆、存款、保险、证券、股权、债务、子女和父母责任逐项表。
- [ ] 同居协议写明月度费用、共同账户、共同购买、登记份额、贷款还款、个人债务、照护、分开后的清算和通知。
- [ ] 居住安排写明房屋权属、居住期限、费用维修、房屋处分限制、失能/死亡/分开居住处理、是否另行登记或公证。
- [ ] 婚前/再婚/婚内协议分别写财产归属、收益、共同购置、债务、第三人告知、子女父母责任、遗嘱/赠与/保险衔接、修改和保存。

### Task 6: 结构审计、渲染审计与最终交付

**Files:**
- Read: `deliverables/晚年IP文书包-v3/*.docx`
- QA: `deliverables/晚年IP文书包-v3/qa-*`

**Steps:**

- [ ] 用 python-docx 检查 6 份文件的章节标题、条款数量、表格数量、签署区和下划线字段。
- [ ] 用 `render_docx.py` 渲染全部 Word 文件。
- [ ] 逐页检查标题、表格、分页、签署区、长下划线、页脚和是否有孤立标题或空白页。
- [ ] 对发现的表格挤压、条款跨页、签署区被拆分等问题修改脚本并重新生成、重新渲染。
- [ ] 执行最终关键字审计，确认每份包含适用边界、具体条款、自查和保存说明。
- [ ] 最终只交付 v3 Word 文件，不交付 QA 图片和中间文件。

**Verification:**

```bash
DOCS=/Users/shaoxinjiang/.codex/plugins/cache/openai-primary-runtime/documents/26.805.11740/skills/documents
PY=/Users/shaoxinjiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
for f in deliverables/晚年IP文书包-v3/*.docx; do
  b=$(basename "$f" .docx)
  "$PY" "$DOCS/render_docx.py" "$f" --output_dir "deliverables/晚年IP文书包-v3/qa-$b"
done
```
