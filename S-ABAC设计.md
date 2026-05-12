# S-ABAC 设计方案

## 1. 设计目标

当前项目已经具备基础的 S-ABAC 能力，能够围绕以下维度进行权限评估：

- 角色匹配（Role-based）
- 权限等级与敏感度匹配（Clearance-based）
- 场景约束（Scenario-based）
- 动作约束（Action-based）

但现有模型仍然偏“平铺式资源控制”，还不能很好支持以下业务目标：

1. 同一个部门中，不同岗位人员权限不同。
2. 不必为所有低敏 Agent 和 Tool 编写细粒度显式策略，高敏对象重点控制即可。
3. Agent 代表一类工作职责，Tool 应归类到 Agent 下，并继承 Agent 的权限控制，必要时再对 Tool 做细化约束。
4. 权限判断不仅要看“用户能不能访问”，还要看“当前任务场景是否适合访问该 Agent 或 Tool”。

因此，需要构建一套更贴近业务职责和任务场景的 S-ABAC 模型。

---

## 2. 设计原则

本方案遵循以下原则：

### 2.1 岗位优先，而不是部门优先

部门只表示组织归属，不直接代表权限边界。
真正决定权限差异的是岗位（Job Role），例如同为 HR 部门，`hr_specialist` 和 `hr_manager` 的权限应不同。

### 2.2 Agent 是职责边界，Tool 是能力实现

Agent 代表一类职责域，例如 HR、Communication、Risk、Document。
Tool 是该职责域下的具体能力，默认继承 Agent 的权限边界，仅在更高敏感或更高风险时单独收紧。

### 2.3 低敏开放，高敏收口

不需要对所有 Agent 和 Tool 编写显式策略。
对于低敏、低风险、可逆操作，允许通过默认基线放行。
对于高敏、不可逆、跨部门、批量处理、外发类对象，必须显式管控。

### 2.4 场景必须匹配职责

即使用户本身具备某类权限，如果当前任务场景与目标 Agent / Tool 的职责不匹配，也不能访问。
例如：拥有文档生成权限的用户，在“市场调研”场景下不应调用 HR 的薪资证明工具。

### 2.5 大模型辅助场景判断，但不直接决定授权

大模型可以辅助识别当前任务场景与目标 Agent / Tool 是否匹配。
但最终授权结果仍由规则引擎决定，大模型只提供“场景适配性判断”，不直接放权。

---

## 3. 核心模型设计

S-ABAC 仍然保持四元组结构：

- Subject：谁在发起访问
- Object：被访问的 Agent / Tool / Resource
- Scenario：当前任务场景
- Action：本次操作行为

### 3.1 Subject（主体）

主体属性建议分为两层：

#### 组织属性

- `department`：所属部门，如 HR、Office、Risk、Engineering
- `job_role`：岗位，如 `hr_specialist`、`hr_manager`、`communication_officer`、`risk_analyst`

#### 权限属性

- `clearance_level`：权限等级，建议 1-5
- `trust_level`：信任等级
- `grants`：岗位附加授权能力，如 `salary_read`、`external_send`、`risk_export`

其中：

- `department` 用于标识业务归属和默认职责边界
- `job_role` 用于区分同部门不同岗位权限
- `grants` 用于补充岗位级能力，不考虑个人例外授权

---

### 3.2 Object（客体）

客体分为两层：

#### Agent Object

Agent 表示一个职责域，定义该类工作的权限边界，例如：

- `RemoteHRAssistantAgent`
- `RemoteCommunicationAgent`
- `RemoteBusinessRiskAgent`
- `RemoteDocumentGeneratorAgent`

Agent 的核心属性包括：

- `department_domain`
- `allowed_job_roles`
- `sensitivity`
- `scenario_tags`
- `allowed_operation_modes`

#### Tool Object

Tool 是某个 Agent 下的具体能力，必须归属到某个 Agent 或职责域。
Tool 默认继承所属 Agent 的权限边界，并可定义更细粒度的覆盖属性。

Tool 建议增加属性：

- `owner_agent`
- `capability_domain`
- `sensitivity`
- `allowed_operation_modes`
- `scenario_tags`
- `requires_approval`
- `max_amount`
- `require_working_hours`
- `require_internal_network`

---

### 3.3 Scenario（场景）

现有场景模型主要关注环境约束，例如工作时间、内外网、风险等级。
新的方案中，Scenario 需要扩展为“任务场景 + 环境场景”的组合。

#### 任务场景属性

- `task_type`：任务类型，如 HR 服务、通知发送、风险分析、文档生成、调研
- `business_goal`：任务目标
- `data_scope`：数据范围，如本人、本部门、跨部门、全公司
- `operation_mode`：操作模式，如 `read`、`generate`、`update`、`send`、`approve`、`delegate`
- `scenario_tags`：场景标签，如 `salary_query`、`employee_proof`、`mass_notification`
- `expected_capabilities`：该场景允许访问的能力域集合

#### 环境场景属性

- `risk_profile`
- `working_hours`
- `network_zone`
- `authentication_strength`

#### 调用链属性

- `delegation_chain`：用户 -> Agent -> Tool 的调用链

---

### 3.4 Action（动作）

动作用于描述本次访问行为，建议保留并规范为：

- `verb`：如 `orchestrate`、`execute`
- `action_type`：如 `delegate`、`call`、`query`、`write`、`send`
- `amount`：金额类阈值
- `irreversible`：是否不可逆
- `batch_size`：批量处理规模
- `target_scope`：本次访问目标范围

---

## 4. 权限判定逻辑

整体权限判定建议按以下顺序执行：

### 4.1 第一步：识别任务场景

从用户请求、上下文、历史调用链中提取当前任务场景，得到：

- `task_type`
- `business_goal`
- `data_scope`
- `operation_mode`
- `scenario_tags`

### 4.2 第二步：进行场景适配性判断

判断当前任务场景是否适合访问目标 Agent / Tool。
这一层不是传统 ABAC 中的静态属性匹配，而是“任务目标与职责域是否一致”的判定。

例如：

- “查询员工薪资”场景适合 HRAgent 及其薪资工具
- “市场调研”场景适合 ResearchAgent，不适合访问 HR 高敏工具
- “群发通知”场景适合 CommunicationAgent，但不适合访问 Risk 工具

### 4.3 第三步：检查岗位权限

判断主体的 `department + job_role + grants` 是否具备访问该职责域的权限。

### 4.4 第四步：检查 Agent 权限

如果目标是 Agent，则检查该 Agent 是否允许当前岗位在当前场景下被调度。

### 4.5 第五步：检查 Tool 继承与覆盖策略

如果目标是 Tool，则先继承所属 Agent 的权限基线，再判断 Tool 是否有额外收紧规则。

### 4.6 第六步：检查硬性约束

包括：

- `clearance_level >= sensitivity`
- `allowed_job_roles`
- `allowed_operation_modes`
- `require_working_hours`
- `require_internal_network`
- `max_amount`
- `irreversible`
- `batch_size`

### 4.7 第七步：决定放行、拒绝或审批

判定结果分为三类：

- `ALLOW`
- `DENY`
- `REVIEW_REQUIRED`

其中审批只用于少量高价值、高风险但业务上合理的场景，不作为高敏资源的默认处理方式。

---

## 5. Agent 与 Tool 的继承模型

### 5.1 设计目标

让 Agent 成为职责域容器，Tool 归属到 Agent 中，减少对每个 Tool 单独配置复杂策略的成本。

### 5.2 继承规则

权限继承顺序建议为：

`Global Baseline -> Department/JobRole Policy -> Agent Policy -> Tool Override -> Scenario Fit`

即：

1. 全局默认基线决定低敏资源如何放行
2. 部门与岗位策略决定主体能访问哪些职责域
3. Agent 策略定义一个职责域的默认边界
4. Tool 策略仅对特殊高敏能力做覆盖
5. 最终还要经过场景适配性判断

### 5.3 默认继承内容

Tool 默认继承所属 Agent 的：

- `department_domain`
- `allowed_job_roles`
- `base_sensitivity`
- `scenario_tags`
- `allowed_operation_modes`

### 5.4 Tool Override 触发条件

只有以下情况才建议为 Tool 单独写更细规则：

- Tool 比所属 Agent 更高敏感
- Tool 涉及不可逆外发
- Tool 涉及写库
- Tool 涉及金额阈值
- Tool 涉及跨部门数据访问
- Tool 涉及批量导出或高价值信息

---

## 6. 场景适配性判断机制

### 6.1 为什么需要场景适配

传统 RBAC 或简单 ABAC 容易出现“有权限就能访问”的问题。
但在 Agent 系统中，更重要的是“当前任务该不该访问这个能力”。

例如：

- Communication 岗位人员可能有文档生成权限，但不应在非通知类任务中调用 HR 证明模板
- HR 岗位人员具备信息查询能力，但不应在普通调研任务中访问薪资工具
- 研究类 Agent 不应作为绕过高敏工具控制的跳板

因此，需要引入“场景适配性判断”。

### 6.2 大模型辅助判断

场景适配性可以交由大模型做辅助判断，但输出必须结构化，建议格式如下：

```json
{
  "fit": "match | mismatch | uncertain",
  "confidence": 0.0,
  "reason": "why",
  "suggested_agent_domains": [],
  "suggested_tool_domains": []
}
```

### 6.3 大模型的职责边界

大模型只负责回答：

- 当前任务属于什么场景
- 当前场景应该使用哪些职责域
- 某个 Agent / Tool 是否与该场景匹配

大模型不负责直接做最终授权结论。

### 6.4 判定策略

- `match`：进入后续规则判定
- `mismatch`：直接拒绝
- `uncertain`：保守处理
  - 低敏资源可继续按规则评估
  - 高敏资源默认拒绝或进入审批

---

## 7. 策略分层设计

### 7.1 基线策略

适用于低敏、低风险、可逆资源。

规则特点：

- 不需要逐个资源写显式策略
- 默认要求 `clearance_level >= sensitivity`
- 如有 `allowed_job_roles`，则岗位必须匹配

### 7.2 职责域策略（Agent 级）

适用于高敏 Agent 或职责边界明显的 Agent。

例如：

- HR Agent 只允许 HR 相关岗位在 HR 场景中调用
- Risk Agent 只允许风险岗位在风控场景中调用
- Communication Agent 只允许通知、沟通、外发场景中调用

### 7.3 Tool 覆盖策略

适用于特殊高敏 Tool。

例如：

- `remote_salary_info_tool`
- `remote_email_tool`
- `save_leave_record`
- `save_travel_record`

这些 Tool 在继承 Agent 策略的基础上，还需要增加：

- 更高敏感度
- 更严格岗位要求
- 金额阈值
- 工作时间约束
- 内网约束
- 审批要求

### 7.4 审批策略

审批不应普遍存在，只用于少量高价值场景，例如：

- 批量外发邮件
- 跨部门批量查询
- 高金额相关操作
- 高敏写入动作
- 高敏导出动作

默认策略应是：

- 不合理场景：直接拒绝
- 合理但高风险场景：进入审批
- 低敏低风险场景：自动放行

---

## 8. 典型场景示例

### 8.1 HR 部门内部差异

同属 HR 部门：

- `hr_specialist`
  - 可查人员基本信息
  - 不可查薪资
  - 可生成普通 HR 说明文档
- `hr_manager`
  - 可查薪资
  - 可生成收入证明
  - 可审批部分 HR 高敏操作

这体现的是“岗位差异”，而不是“部门一致即权限一致”。

### 8.2 高敏 Tool 继承并收紧

`RemoteHRAssistantAgent` 可作为 HR 职责域 Agent。
其下：

- `remote_person_info_tool`：继承 HR Agent 权限，属于高敏读
- `remote_salary_info_tool`：在继承基础上进一步收紧，只允许更高岗位访问

### 8.3 场景不匹配直接拒绝

用户具备文档生成权限，但当前任务是“市场调研”。
此时即使可以访问 `RemoteDocumentGeneratorAgent`，也不能调用“收入证明模板”这类 HR 高敏 Tool。

### 8.4 Communication 场景

`communication_officer`：

- 在“通知发送”场景中可以访问 Communication Agent
- 在“批量外发邮件”场景下可能需要审批
- 在“员工薪资查询”场景下不能访问 HR 工具

### 8.5 Risk 场景

`risk_analyst`：

- 在风控分析任务中可访问 `RemoteBusinessRiskAgent`
- 在普通调研或 HR 任务中不应访问风险高敏能力
- 风控报告导出或跨部门共享需更高限制或审批

---

## 9. 与当前项目实现的映射建议

### 9.1 Subject 扩展

当前已有：

- `role`
- `department`
- `clearance_level`
- `trust_level`

建议新增或替换为：

- `job_role`
- `grants`

并逐步从“role 直接决定权限”迁移到“job_role 决定权限，department 作为组织边界”。

### 9.2 Object 扩展

当前已有：

- `sensitivity`
- `allowed_roles`
- `category`

建议逐步扩展为：

- `owner_agent`
- `department_domain`
- `allowed_job_roles`
- `scenario_tags`
- `allowed_operation_modes`

### 9.3 Scenario 扩展

当前已有：

- `risk_profile`
- `working_hours`
- `network_zone`

建议新增：

- `task_type`
- `business_goal`
- `data_scope`
- `scenario_tags`
- `expected_capabilities`
- `delegation_chain`

### 9.4 Policy Engine 扩展方向

当前策略引擎已经支持：

- Subject/Object/Scenario/Action
- 显式规则
- 默认兜底规则
- 工作时间、网络、金额限制

后续可在此基础上增加两类能力：

1. Agent -> Tool 继承逻辑
2. 场景适配性判断结果接入规则引擎

即在正式评估前，先生成一个 `scenario_fit_result`，并将其纳入策略条件。

---

## 10. 最终判定思路

最终授权不再只依赖“角色 + 敏感度”，而应同时满足以下四类条件：

1. 主体岗位合法
2. 客体职责域合法
3. 当前任务场景匹配
4. 动作与环境约束满足

可以概括为：

> 有岗位权限，不代表一定能访问；
> 有职责访问权，不代表在任何任务场景下都能访问；
> 只有“岗位匹配 + 职责匹配 + 场景匹配 + 约束满足”同时成立时，访问才允许通过。

---

## 11. 总结

这套 S-ABAC 方案的核心价值在于：

- 用“部门 + 岗位”替代简单角色控制，解决同部门权限差异
- 用“Agent 代表职责、Tool 继承 Agent”降低策略维护成本
- 用“高敏对象重点治理”避免对低敏资源过度建模
- 用“场景适配性判断”避免能力误用与职责越界
- 用“大模型辅助场景识别”提升复杂任务下的权限判断准确性

该方案适合作为当前项目 S-ABAC 的下一阶段设计基础，并可在现有代码结构上逐步演进实现。
