# 生前预嘱与健康照护包 v4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 只重做第一份《生前预嘱与健康照护包》，忠实继承用户两份参考 Word 的版式，并让老年人可以看懂、填写、签署和保存。

**Architecture:** 新建一个单文件生成器，不覆盖 v1/v2/v3。生成器把参考手册版式组件和面向老年人的内容组件分开：版式组件固定 A4、字体、行距、提示文字、下划线和 Normal Table；内容组件按“先看懂—填健康事实—写生活照护—写五个愿望—签署保存”顺序输出。

**Tech Stack:** 用户提供的 DOCX 参考文件、Python 3、python-docx、OOXML 表格几何、documents skill 的 render_docx.py、bundled Python runtime。

## Global Constraints

- 页面：A4 纵向；上/下 1 英寸；左/右 1.25 英寸；单栏。
- 正文：宋体 14 磅，黑色，左对齐，固定 24 磅行距，段后 0 磅或仅使用空段分隔。
- 开头警示：36 磅、26 磅、22 磅居中大字层级；不加入推广二维码、蓝色标题、彩色表头或独立页脚。
- 提示文字：宋体 14 磅、灰色，使用“提示：”“先说人话：”“不会填怎么办：”等标签。
- 填写内容：姓名、日期、证件、金额、账户、地址、联系人和自定义愿望使用连续下划线；选择项使用 □。
- 表格：Normal Table 简单网格；不填充颜色、不使用蓝色/绿色表头；表格只承担重复信息、选择/记录和签字/保存记录。
- 输出：新建 deliverables/晚年IP文书包-v4/01-生前预嘱与健康照护包-v4.docx；保留 v1/v2/v3 不变。
- 质量边界：不把公开五个愿望框架写成协会官方文件，不承诺医院必然执行，不把“具体约定：________”作为唯一正式条款。

---

### Task 1: 固定参考版式组件

**Files:**
- Create: scripts/generate_health_care_package_v4.py
- Read: /tmp/late-life-health-v4-20260811/artifact.md
- Read: /Users/shaoxinjiang/Documents/遗嘱业务相关材料/自书遗嘱20260715 版本.docx
- Read: /Users/shaoxinjiang/Desktop/业务资料/非公证继承课程/非公证继承办理手册完整版.docx

**Interfaces:**
- style_doc(doc): 设置章节几何、Normal 和标题样式，不设置运行页脚。
- add_ref_paragraph(doc, text, size=14, bold=False, color=None, align=LEFT, line_pt=24): 创建参考格式的直接格式段落。
- add_ref_heading(doc, text, level=1|2, align=LEFT): 创建 22 磅/16 磅的参考层级标题。
- ref_table(doc, headers, rows, widths): 创建无彩色填充的 Normal Table 简单网格。
- blank_field(doc, label, chars): 输出标签和下划线，不用只有“具体约定”的空白段落。

- [ ] Step 1: Encode reference measurements

  Set page size and margins to 8.268 x 11.693 in, left/right=1.25 in, top/bottom=1.00 in. Set body runs to 宋体 14 pt, fixed 24 pt line spacing, and no footer. Set opening title/warning runs to 36/26/22 pt, centered.

- [ ] Step 2: Encode reference-style tables

  Set each table to Normal Table, no shading, simple grid, autofit-like behavior, and total width close to 8966 DXA with the same negative table indent family as the reference. Use compact first and last columns only for step/status tables; reserve the center column for explanation.

- [ ] Step 3: Add non-content QA helpers

  Implement plain_language_note, signature_line, witness_block, handover_table, and self_checklist using the same 14 pt body rhythm. Keep professional terms in the text component, not as unexplained table headers.

- [ ] Step 4: Compile the builder

    /Users/shaoxinjiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m py_compile scripts/generate_health_care_package_v4.py

  Expected: exit code 0.

### Task 2: Write elderly-reader content and forms

**Files:**
- Modify: scripts/generate_health_care_package_v4.py
- Output: deliverables/晚年IP文书包-v4/01-生前预嘱与健康照护包-v4.docx

**Interfaces:**
- build_health_package(doc): 按“先看懂”→“怎么用”→“健康说明书”→“生活照护意愿书”→“我的五个愿望”→“签字见证保存”→“家属沟通和更新”输出全文。

- [ ] Step 1: Add the plain-language opening

  Explain in 14 pt body text:
  - 生前预嘱是“趁现在还能清楚表达，把将来可能需要的医疗和照护想法提前写下来”；
  - this package is not a medical record, doctor’s order, bank authorization, or will;
  - who should fill each part, when to use it, and where to take it after signing;
  - the five-step process and the “不会填怎么办” path: stop, ask family/doctor, write “先听医生解释再决定”.

- [ ] Step 2: Add the personal health information sheet

  Add a short explanation before each section: basic information and ability to do daily tasks; chronic illness as “长期需要关注的疾病”; medicines and allergies as “吃了某种药/食物可能出现危险反应”; usual hospital/doctor; major history; daily care risks; first and second emergency contacts; and an after-filling instruction for storing medicine boxes, reports, insurance information and the form.

- [ ] Step 3: Add the personal daily-care preference sheet

  Cover home/family/elder-care residence, food texture, wake/sleep, bathing, mobility, visits, privacy, music, faith, caregiver preferences and “I do not want to be treated this way”. Every choice group must include a line for the person’s most important need and an alternative if the preferred arrangement cannot be met.

- [ ] Step 4: Add the five wishes with definitions

  Use plain-language headings for: medical services; treatments that only keep the body going; how others should treat the person; what family and friends should know; and who helps communicate wishes when the person cannot express them. Define 生命末期、不可逆昏迷、持续植物状态、舒缓治疗、协助决策人 immediately before the choices. Include “□先听医生解释再决定” in each medical choice group and state that the helper is not automatically an heir or property disposer.

- [ ] Step 5: Add signatures, witnesses, storage and updates

  Include two witness statements, witness identity/contact fields,本人 signature, witness signatures, update/revocation log, original/copy/electronic storage record, hospital/family notice record, and family discussion record. State that a witness confirms the signing scene and the person’s apparent clarity, not medical results or legal validity.

- [ ] Step 6: Generate the v4 DOCX

    /Users/shaoxinjiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/generate_health_care_package_v4.py

  Expected: only 01-生前预嘱与健康照护包-v4.docx is newly generated in the v4 deliverable folder.

### Task 3: Structural and visual verification

**Files:**
- Read: deliverables/晚年IP文书包-v4/01-生前预嘱与健康照护包-v4.docx
- QA output: /tmp/late-life-health-v4-20260811/final-render/

- [ ] Step 1: Run text and form audit

  Inspect paragraphs and table cells with bundled Python. Require: 生前预嘱、什么时候用、谁来填、填完以后、不会填怎么办、慢性病、过敏、紧急联系人、生命末期、舒缓治疗、协助决策人、见证人、原件、复印件、电子版、撤回、更新; at least 2 witness blocks; at least 10 underline fields; and no generic-only 具体约定：________________.

- [ ] Step 2: Run page render

    PY=/Users/shaoxinjiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
    DOCS=/Users/shaoxinjiang/.codex/plugins/cache/openai-primary-runtime/documents/26.805.11740/skills/documents
    "$PY" "$DOCS/render_docx.py" "deliverables/晚年IP文书包-v4/01-生前预嘱与健康照护包-v4.docx" --output_dir "/tmp/late-life-health-v4-20260811/final-render"

- [ ] Step 3: Inspect every rendered page

  Open every page image and check: no blue/green v3 table fills, no running footer, large readable Chinese text, headings do not get stranded, table columns remain inside the page, underlines are visible, signature blocks stay together, and no page is an unexplained blank.

- [ ] Step 4: Run style and section audit

    "$PY" "$DOCS/scripts/section_audit.py" "deliverables/晚年IP文书包-v4/01-生前预嘱与健康照护包-v4.docx"
    "$PY" "$DOCS/scripts/style_lint.py" "deliverables/晚年IP文书包-v4/01-生前预嘱与健康照护包-v4.docx" --json "/tmp/late-life-health-v4-20260811/final-style.json"
    unzip -t "deliverables/晚年IP文书包-v4/01-生前预嘱与健康照护包-v4.docx"

- [ ] Step 5: Verify old outputs were not overwritten

  Confirm that the v1, v2 and v3 DOCX files still exist and that only the new v4 DOCX is part of the new deliverable set.
