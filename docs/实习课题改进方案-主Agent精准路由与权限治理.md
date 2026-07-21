# SuperAgent 实习课题改进方案

## 1. 方案定位

本方案面向“数字员工的智能任务执行关键技术研究”，在现有 SuperAgent 代码基础上增量改造，不推倒重写。

项目最终要形成的不是部门 Agent 之间自由互调的网络，而是一个中心化、能力强、可治理的主 Agent：

1. 主 Agent 接收用户自然语言。
2. 主 Agent 准确理解业务意图、对象、约束和风险。
3. 权限引擎在路由前计算用户可以访问的部门 Agent、数据和动作。
4. 主 Agent 在合法候选中选择最匹配的部门级大 Agent。
5. 部门 Agent 只能通过主 Agent 接收任务，不能直接调用其他部门 Agent。
6. 部门 Agent 调用 MCP 工具时再次鉴权。
7. 最终结果经过校验、脱敏和审计后返回用户。

核心研究主线可以概括为：

> 任务理解与结构化表达 → 权限感知的部门 Agent 精准路由 → 受控工具执行 → 结果治理与任务闭环。

## 2. 当前项目基线

### 2.1 已有能力

当前项目已经具备以下基础：

- 基于 LangGraph 的 Coordinator、Planner、Publisher、AgentProxy 工作流。
- Agent 注册和动态加载机制。
- 本地工具、远程 Agent、MCP 工具注册机制。
- Launch、Polish、Production 三种工作模式。
- 任务日志、检查点和恢复能力。
- FastAPI、SSE 和 Web Studio 页面。
- 初步的任务场景识别和结构化字段。
- S-ABAC 权限模型及 Agent、工具两级执行拦截。
- 用户、Agent、资源属性展示及权限预检查页面。

当前预览状态：

- 首页可通过 `http://127.0.0.1:8001` 访问。
- S-ABAC 预览模式可以开启。
- 当前静态配置包含 6 个演示用户、14 个 Agent 安全属性、26 个资源安全属性和 3 条显式策略。
- 本地 4 个 Agent、14 个远程 Agent 已成功注册，页面接口当前可读取 18 个 Agent。
- 主模型已接入阿里云百炼：主工作流使用 `deepseek-v4-flash`，视觉模型保留 `qwen3.5-27b`。
- `qwen3.5-27b` 和 `deepseek-v4-flash` 的最小 API 调用均已验证成功。
- 报告任务已完整经过 Coordinator、Planner、Publisher、AgentProxy、S-ABAC、Reporter 并正常结束。
- HR 人员查询已完整经过主工作流、RemoteHRAssistantAgent 和远程工具服务。

实测暴露的问题：

- `qwen3.5-27b` 用作 Publisher 时曾直接回答用户问题，没有遵守 `{"next":"agent"}` 协议，说明路由不能只靠 Prompt 约束。
- Publisher 原先写入自然语言状态，无法可靠判断 Agent 已执行，可能重复派发。
- AgentProxy 原先将字典直接写入消息 content，与当前 LangChain/Pydantic 消息类型不兼容。
- 远程 HR Demo 中薪资工具能查到“张三”，人员基本信息工具却返回 0 条，Mock 数据和匹配逻辑不一致。
- `/api/tools` 当前主要展示全局工具，远程 Agent 自带工具没有完整计入页面工具总数。
- `config/mcp_sources.json` 指向 `127.0.0.1:8000/sse` 的 Excel MCP，但对应服务未启动，因而启动日志仍有 MCP 热加载失败告警。

### 2.2 当前真实执行方式

现有主链路是：

```text
用户请求
  → Coordinator 判断是否需要规划
  → Planner 从可见 Agent 中生成 steps
  → Publisher 按 steps 顺序取下一个 Agent
  → AgentProxy 调用具体 Agent
  → Agent 调用本地工具或 MCP
  → 返回结果
```

其中 Planner 负责选择 Agent；Publisher 主要负责按计划推进，并不是完整的语义路由器。

### 2.3 当前权限方式

现有 S-ABAC 使用四类属性进行判断：

- Subject：用户角色、部门、岗位、密级、信任等级、grants。
- Object：Agent 或工具的职责、敏感级别、所需角色和授权。
- Scenario：任务类型、业务目标、数据范围、场景标签、环境信息。
- Action：查询、读取、写入、发送、执行、金额、是否不可逆等。

现有权限控制点包括：

1. Planner 前按 `available_agents` 粗粒度过滤候选 Agent。
2. AgentProxy 派发前执行 `enforce_agent_dispatch`。
3. 工具调用前通过 `SecureToolWrapper` 执行 `enforce_tool_call`。
4. PolicyEngine 输出 `ALLOW`、`DENY` 或 `REVIEW_REQUIRED`。

### 2.4 当前主要问题

#### 启动和配置问题

- 项目声明 Python 3.12，但本地实际使用 Python 3.14。
- 完整依赖集合存在版本回溯和 MCP 版本约束冲突。
- `click` 被代码使用，但未声明为直接依赖。
- LLM 客户端在模块导入时创建，没有 API Key 时连页面也无法启动。
- Tavily 工具也在导入时创建，没有 Tavily Key 会导致 Agent、Tool 接口整体失败。
- Python 3.14 下存在 LangChain Pydantic V1 兼容警告。

#### 路由问题

- 当前 Planner 一次生成完整多 Agent 计划，任务理解、Agent 选择和执行编排耦合在一个 Prompt 中。
- Agent 选择主要依赖名称和自然语言描述，缺少标准化 AgentCard。
- 没有独立、可评测的 `RoutingDecision`。
- 没有 Top-K 候选、候选得分和可解释路由原因。
- 对低置信度、歧义请求和越权请求缺少统一决策门。
- Publisher 只是执行计划推进器，无法承担动态纠偏和重新路由职责。

#### 权限问题

- 用户权限来自静态 Demo 字典，尚未抽象成可替换的身份和组织接口。
- 路由前只有 `available_agents` 粗过滤，细粒度权限主要在选中 Agent 后检查。
- 未形成任务级、短时、最小权限的委托凭证。
- `coor_agents` 等显式候选输入可能绕过前置粗过滤；S-ABAC 关闭时风险更大。
- 数据范围主要依赖文本推断，缺少字段级、记录级和用途级约束。
- `REVIEW_REQUIRED` 与工作流暂停、人工审批、恢复执行尚未完整贯通。
- 缺少返回结果的敏感信息识别、脱敏和防泄露控制点。

#### 闭环和评测问题

- 缺少统一的任务完成判定标准。
- 缺少路由准确率、越权阻断率、工具调用成功率等评测数据集。
- 失败恢复更多是技术检查点恢复，尚未形成业务补偿、重试和转人工策略。
- 页面能够展示执行信息，但还不能完整解释“为何路由、为何允许、为何拒绝”。

## 3. 目标总体架构

```text
用户 / Web / API
      │
      ▼
身份与会话上下文
      │
      ▼
主 Agent（唯一跨部门入口）
  ├─ 任务理解器 Task Profiler
  ├─ 候选召回 Candidate Retriever
  ├─ 权限决策 Policy Decision Point
  ├─ 路由器 Department Router
  ├─ 置信度门 Confidence Gate
  ├─ 任务拆分与调度 Orchestrator
  └─ 结果验证与汇总 Result Governor
      │
      ▼
部门 Agent 网关
  ├─ 人力资源 Agent
  ├─ 综合办公 Agent
  ├─ 培训学习 Agent
  ├─ 差旅服务 Agent
  ├─ 会议协同 Agent
  ├─ 通信消息 Agent
  ├─ 知识制度 Agent
  ├─ 文档处理 Agent
  ├─ 科技支持 Agent
  └─ 风险合规 Agent
      │
      ▼
工具权限执行点 PEP
      │
      ▼
MCP / 内部模拟服务 / 本地工具
      │
      ▼
输出校验、脱敏、审计、任务闭环
```

架构约束：

- 只有主 Agent 可以调度部门 Agent。
- 部门 Agent 不能直接发现或调用其他部门 Agent。
- 跨部门任务由主 Agent 拆分、按依赖顺序调度，并最小化跨部门数据传递。
- 权限引擎给出硬约束；大模型不能自行决定绕过权限。
- 每次任务和工具调用都必须携带同一个 `trace_id`。

## 4. 主 Agent 改造设计

### 4.1 把任务理解从 Planner 中独立出来

新增 `TaskProfile`，将自然语言稳定转换成结构化对象：

```json
{
  "task_id": "task_xxx",
  "intent": "employee_information_query",
  "task_type": "HR",
  "business_goal": "查询员工部门和联系方式",
  "action": "read",
  "entities": {
    "employee_name": "张三"
  },
  "data_scope": ["employee.basic_profile", "employee.contact"],
  "scenario_tags": ["person_lookup"],
  "expected_capabilities": ["EmployeeService"],
  "risk_level": "MEDIUM",
  "irreversible": false,
  "constraints": [],
  "missing_fields": [],
  "confidence": 0.93
}
```

结构化过程采用“规则兜底 + LLM 抽取 + Schema 校验”：

1. 规则识别明显动作、日期、金额、人员、部门和敏感词。
2. LLM 负责语义归一和隐含意图识别。
3. Pydantic Schema 校验字段类型和枚举值。
4. 缺失关键字段时不进入执行，转为澄清。

### 4.2 建立标准 AgentCard

每个部门 Agent 必须注册统一能力卡，而不是只提供一段 description：

```json
{
  "agent_id": "hr_department_agent",
  "name": "人力资源数字员工",
  "department": "HR",
  "capabilities": ["EmployeeService", "LeaveService", "SalaryService"],
  "intents": ["employee_information_query", "leave_query", "salary_query"],
  "supported_actions": ["read", "create", "update"],
  "accepted_data_scopes": ["employee.basic_profile", "employee.salary"],
  "risk_ceiling": "HIGH",
  "required_grants": ["employee_profile_read"],
  "tool_scopes": ["hr.*"],
  "input_schema": {},
  "output_schema": {},
  "version": "1.0.0",
  "status": "ONLINE"
}
```

AgentCard 同时服务于：

- 候选召回。
- 权限过滤。
- 路由打分。
- 输入输出校验。
- 页面能力展示。
- Agent 版本治理。

### 4.3 权限感知的候选召回

候选选择分成硬过滤和软排序两个阶段。

硬过滤必须先于大模型路由：

```text
全部部门 Agent
  ∩ 在线 Agent
  ∩ 支持目标 intent/action 的 Agent
  ∩ 用户有权访问的 Agent
  ∩ Agent 能处理目标数据范围
  ∩ 场景和风险约束允许
  = 合法候选集合
```

大模型只能在合法候选集合中选择，不能看到或选择不允许的 Agent。

### 4.4 混合路由算法

建议采用“规则 + 向量召回 + LLM 重排”的混合方式：

1. 规则命中：处理高确定性的业务词和系统命令。
2. 向量召回：使用 TaskProfile 与 AgentCard 的语义相似度召回 Top-K。
3. 权限硬过滤：剔除越权候选。
4. LLM 重排：比较剩余候选与用户真实目标。
5. 确定性校验：校验输出 Agent 必须存在于合法候选集合。

初始评分可采用：

```text
RouteScore =
  0.35 × 意图匹配
  + 0.25 × 能力匹配
  + 0.15 × 场景匹配
  + 0.10 × 数据范围匹配
  + 0.10 × 历史成功率
  + 0.05 × 时延和成本得分
```

权限不进入加权分数，而是硬约束。没有权限时，无论语义得分多高都不能路由。

### 4.5 标准 RoutingDecision

主 Agent 每次路由都输出可审计对象：

```json
{
  "decision_id": "route_xxx",
  "task_id": "task_xxx",
  "selected_agent": "hr_department_agent",
  "candidate_agents": [
    {"agent_id": "hr_department_agent", "score": 0.94}
  ],
  "decision": "DISPATCH",
  "confidence": 0.94,
  "reason_codes": ["INTENT_MATCH", "CAPABILITY_MATCH", "AUTHORIZED"],
  "required_grants": ["employee_profile_read"],
  "excluded_agents": [
    {"agent_id": "risk_agent", "reason": "CAPABILITY_MISMATCH"}
  ],
  "trace_id": "trace_xxx"
}
```

### 4.6 置信度决策门

建议初始阈值：

- `confidence >= 0.80`：自动派发。
- `0.55 <= confidence < 0.80`：向用户澄清或展示建议部门。
- `confidence < 0.55`：不派发，要求补充信息。
- 命中高风险动作：即使置信度高，也必须进行二次确认或审批。
- 合法候选为空：明确返回权限或能力不足原因，不能随意选择通用 Agent。

阈值不能凭经验定稿，需要用评测集调整。

## 5. 权限治理改造设计

### 5.1 保留并升级 S-ABAC

保留现有 `Subject + Object + Scenario + Action` 模型，并按照标准访问控制组件拆分：

- PIP：属性信息点，提供用户、组织、Agent、工具、环境属性。
- PDP：权限决策点，运行 S-ABAC PolicyEngine。
- PEP：权限执行点，部署在路由、AgentProxy、工具包装器和输出层。
- PAP：策略管理点，负责策略配置、版本和发布。

### 5.2 四道权限闸门

#### 闸门一：路由前

判断用户是否有资格看到和使用某部门 Agent，输出合法候选集合。

#### 闸门二：派发前

判断本次任务的动作、数据范围、用途、风险和环境是否允许交给目标 Agent。

#### 闸门三：工具调用前

部门 Agent 每调用一次 MCP 都重新检查：

- 工具是否属于该 Agent。
- 用户是否有相应 grant。
- 输入数据范围是否越界。
- 是否工作时间、内部网络或审批状态满足要求。
- 写入、发送和不可逆操作是否需要二次确认。

#### 闸门四：结果返回前

对结果进行：

- 字段级脱敏。
- 敏感数据识别。
- 超范围字段删除。
- 外发风险检查。
- 审计摘要生成。

### 5.3 有效权限计算

```text
EffectivePermission =
  UserPermission
  ∩ AgentBoundary
  ∩ TaskScope
  ∩ ToolPolicy
  ∩ ContextPolicy
```

主 Agent 是系统调度者，但不能因为自身权限高就替用户放大权限。所有部门 Agent 和工具调用必须继承原始用户身份。

### 5.4 任务级委托凭证 DelegationGrant

主 Agent 派发时签发短时、最小权限的内部凭证：

```json
{
  "grant_id": "grant_xxx",
  "task_id": "task_xxx",
  "trace_id": "trace_xxx",
  "subject_id": "user_001",
  "delegate_agent": "hr_department_agent",
  "allowed_actions": ["read"],
  "allowed_data_scopes": ["employee.basic_profile"],
  "allowed_tools": ["hr.person.query"],
  "purpose": "person_lookup",
  "expires_at": "2026-07-15T16:30:00+08:00",
  "max_calls": 3,
  "approval_id": null
}
```

部门 Agent 不能扩大 `DelegationGrant`，也不能把凭证转给其他 Agent。

### 5.5 审批闭环

将 `REVIEW_REQUIRED` 从单纯返回值改造成真正的工作流状态：

```text
策略要求审批
  → 保存当前 Checkpoint
  → 创建审批单
  → 页面展示待审批事项
  → 批准 / 拒绝
  → 批准后签发带 approval_id 的 DelegationGrant
  → 从原检查点恢复
```

审批需要防止重复消费、过期审批和任务参数被审批后修改。

## 6. 部门 Agent 与演示场景规划

### 6.1 建议的 10 个部门级或治理级 Agent

| Agent | 主要职责 | 对应场景 |
|---|---|---|
| MainOrchestratorAgent | 意图理解、权限感知路由、跨域调度 | 全部 |
| HRDepartmentAgent | 人员基本信息、组织关系、人事服务 | 人员查询 |
| OfficeDepartmentAgent | 日程、待办、办公流程 | 日程查询 |
| LearningDepartmentAgent | 培训课程、学习记录 | 课程检索 |
| TravelDepartmentAgent | 出差申请、行程、标准查询 | 差旅服务 |
| MeetingDepartmentAgent | 会议室、参会人、会议安排 | 会议安排 |
| CommunicationDepartmentAgent | 站内信、邮件、通知 | 消息发送 |
| KnowledgeDepartmentAgent | 制度、知识库、业务问答 | 知识检索 |
| DocumentDepartmentAgent | Word、表格、报告生成 | 文档生成 |
| RiskComplianceAgent | 权限复核、敏感操作、合规检查 | 高风险治理 |

如需严格区分“部门 Agent”和“系统治理 Agent”，可将 MainOrchestratorAgent、RiskComplianceAgent 记为系统 Agent，其余补充 TechnologySupportAgent 和 FinanceServiceAgent，保证部门 Agent 数量达到 10。

### 6.2 30 个以上 MCP 工具规划

每个场景至少准备 4～6 个模拟工具：

| 工具域 | 示例工具 |
|---|---|
| HR | 人员搜索、人员详情、组织关系、联系方式、薪资查询 |
| Calendar | 查询日程、空闲时间、创建日程、修改日程、取消日程 |
| Learning | 课程搜索、课程详情、报名、取消报名、学习记录 |
| Travel | 标准查询、行程推荐、预算估算、申请创建、申请查询、申请取消 |
| Meeting | 会议室搜索、空闲查询、参会人查询、会议创建、会议修改、会议取消 |
| Communication | 联系人查询、消息草稿、站内信发送、邮件发送、通知发送、发送状态查询 |
| Knowledge | 制度搜索、知识详情、引用校验、版本查询 |
| Document | DOCX 生成、表格生成、模板查询、文件保存 |

工具不必全部连接真实银行系统，可以使用可重复的 Mock MCP 服务，但输入输出 Schema、权限属性、异常码和审计记录必须完整。

## 7. 页面改进方案

### 7.1 主页面增加“主 Agent 决策台”

展示：

- 原始用户请求。
- TaskProfile 结构化结果。
- 候选 Agent 及匹配分数。
- 因权限被排除的候选及原因。
- 最终选中部门 Agent。
- 路由置信度和决策理由。

### 7.2 执行时间线

统一展示：

```text
任务理解 → 候选召回 → 权限过滤 → Agent 派发 → 工具调用 → 结果校验 → 完成
```

每一步显示耗时、输入摘要、输出摘要、决策和错误。

### 7.3 权限与审批页面

增加：

- 当前用户身份和组织属性。
- 有权访问的 Agent、工具和数据范围。
- 路由前预检查。
- 拒绝事件列表。
- 待审批、已批准、已拒绝和已过期事项。
- 对应 Policy、reason code 和 trace_id。

### 7.4 Agent 注册中心页面

展示 AgentCard、状态、版本、能力、工具、输入输出 Schema、历史成功率和平均时延。

## 8. 可观测性与闭环保障

### 8.1 统一 Trace

所有事件至少包含：

- `trace_id`
- `task_id`
- `user_id`
- `routing_decision_id`
- `agent_id`
- `tool_call_id`
- `policy_decision_id`
- `approval_id`

### 8.2 标准失败分类

- `TASK_UNCLEAR`
- `NO_CAPABLE_AGENT`
- `PERMISSION_DENIED`
- `APPROVAL_REQUIRED`
- `AGENT_UNAVAILABLE`
- `TOOL_TIMEOUT`
- `TOOL_VALIDATION_FAILED`
- `OUTPUT_POLICY_VIOLATION`
- `RESULT_INCOMPLETE`

### 8.3 恢复策略

- 瞬时网络错误：指数退避重试。
- Agent 不可用：主 Agent 在合法候选中重新路由。
- 工具不可用：同一部门 Agent 内选择等价工具。
- 参数缺失：向用户澄清，不盲目补全。
- 不可逆操作：禁止自动重试。
- 跨部门任务部分成功：执行补偿或明确返回部分完成状态。

## 9. 评测方案

### 9.1 数据集

围绕 6 个任务书场景建立至少 180 条样例：

- 每个场景 20 条正常表达。
- 每个场景 4 条口语、省略或错别字表达。
- 每个场景 3 条跨域或复合请求。
- 每个场景 3 条越权、提示注入或高风险请求。

每条样例标注：

- 标准 TaskProfile。
- 正确部门 Agent。
- 允许或拒绝结果。
- 必要工具。
- 是否需要澄清或审批。
- 预期结果字段。

### 9.2 核心指标

| 指标 | 建议目标 |
|---|---:|
| TaskProfile 关键字段准确率 | ≥ 90% |
| 部门 Agent Top-1 路由准确率 | ≥ 90% |
| Top-3 召回率 | ≥ 98% |
| 越权 Agent 路由阻断率 | 100% |
| 越权工具调用阻断率 | 100% |
| 高风险操作审批触发率 | 100% |
| 六类场景端到端完成率 | ≥ 85% |
| 无效多 Agent 调用率 | ≤ 5% |
| 失败原因可解释覆盖率 | 100% |

准确率目标需要结合 Mock 服务稳定性和模型能力逐步调整，不能只展示成功案例。

### 9.3 对照实验

至少比较三组：

1. 原始 Planner 直接选择 Agent。
2. 仅使用语义相似度路由。
3. 结构化理解 + 权限硬过滤 + 混合路由。

比较路由准确率、越权率、调用次数、时延和 Token 成本，形成课题的实验结论。

## 10. 五周实施计划

### 第 1 周：跑通基线与建立评测基准

- 解决环境依赖、导入时强制 Key、可选工具降级问题。
- 固化一条启动命令和环境检查接口。
- 梳理现有 Agent、工具、工作流和 S-ABAC。
- 定义 TaskProfile、AgentCard、RoutingDecision Schema。
- 完成 6 场景首批标注样例。

交付物：可复现环境、基线分析、三类 Schema、首批数据集、基线运行录像。

### 第 2 周：主 Agent 精准路由

- 将任务理解器从 Planner 解耦。
- 实现 AgentCard 注册和候选召回。
- 实现规则、语义和 LLM 混合路由。
- 实现置信度门、澄清和单部门优先策略。
- 页面展示 TaskProfile 和 RoutingDecision。

交付物：主 Agent 路由原型、路由解释页面、路由准确率基线。

### 第 3 周：权限感知路由与任务级授权

- 将 S-ABAC 前移到候选过滤阶段。
- 抽象 IdentityProvider 和属性接口。
- 实现 DelegationGrant。
- 贯通 Agent、工具两级鉴权。
- 完成审批暂停、批准和恢复执行。
- 增加输出脱敏控制点。

交付物：四道权限闸门、权限演示矩阵、越权案例、审批闭环。

### 第 4 周：10+ Agent、30+ MCP 与闭环

- 建立部门 Agent 和 Mock MCP 服务。
- 覆盖人员、日程、课程、差旅、会议、消息 6 个场景。
- 完成输入输出 Schema 校验。
- 增加失败重试、重新路由、补偿和转人工策略。
- 完成统一 Trace 和执行时间线。

交付物：不少于 10 个 Agent、30 个 MCP 工具、六场景端到端 Demo。

### 第 5 周：实验、优化和材料沉淀

- 完成 180 条以上评测集。
- 运行三组对照实验。
- 分析路由、权限、任务成功率、时延和成本。
- 完成技术报告、用户手册、部署说明和汇报 PPT。
- 录制典型成功、澄清、拒绝、审批和失败恢复案例。

交付物：评测报告、最终 Demo、课题报告、PPT、演示视频。

## 11. 代码改造落点

建议新增：

```text
src/orchestrator/
  main_agent.py
  task_profiler.py
  candidate_retriever.py
  department_router.py
  confidence_gate.py
  result_governor.py

src/contracts/
  task_profile.py
  agent_card.py
  routing_decision.py
  delegation_grant.py

src/security/
  identity_provider.py
  candidate_filter.py
  delegation.py
  output_guard.py

src/evaluation/
  dataset.py
  routing_metrics.py
  security_metrics.py
  end_to_end_runner.py
```

重点修改：

- `src/workflow/coor_task.py`：将 Planner 的选择职责迁移给主 Agent 路由器。
- `src/workflow/process.py`：用权限引擎生成合法候选，不再只依赖静态 `available_agents`。
- `src/manager/registry.py`：保存和查询 AgentCard。
- `src/security/enforcement.py`：接收 RoutingDecision 和 DelegationGrant。
- `src/security/policy.py`：返回结构化 reason code 和 obligations。
- `src/security/tool_wrapper.py`：验证任务级委托范围。
- `src/security/approval.py`：与 Checkpoint 恢复流程贯通。
- `src/service/web_app.py`：增加路由、审批、审计和评测接口。
- `web/app.js`、`web/security.js`：增加主 Agent 决策台和执行治理视图。
- `src/llm/llm.py`：改为惰性加载，页面启动不再强制要求全部模型 Key。
- `src/tools/search.py`：缺少 Tavily Key 时将搜索标记为不可用，而不是导致整个注册中心失败。
- `pyproject.toml`：整理 Python 3.14 实际依赖和可选依赖分组。

## 12. 实施优先级

### P0：先保证可复现

- 页面无 Key 可启动。
- 可选工具缺 Key 时优雅降级。
- Agent、Tool、Security 接口可正常查看。
- 依赖安装不发生长时间回溯。

### P1：形成课题核心创新点

- TaskProfile。
- AgentCard。
- 权限感知候选过滤。
- 混合路由。
- RoutingDecision 和可解释页面。

### P2：强化银行场景治理

- DelegationGrant。
- 数据范围控制。
- 人工审批闭环。
- 输出脱敏。
- 全链路审计。

### P3：扩大 Demo 和完成实验

- 10+ Agent。
- 30+ MCP。
- 六场景闭环。
- 评测集与对照实验。

## 13. 第一轮代码修改建议

页面确认完成后，第一轮只做 P0，不立即重写路由：

1. LLM 改成惰性初始化。
2. Tavily 等可选工具改成缺配置时降级。
3. 整理 Python 3.14 依赖，补充 `click`，拆分可选依赖。
4. 增加 `/api/health/ready`，区分页面、模型、Agent、MCP 的状态。
5. 保证 `/api/agents`、`/api/tools`、`/api/security/*` 无 Key 时也能展示。
6. 在页面显式显示“页面可用、模型未配置、搜索未配置”，避免 500。

这轮完成后再进入主 Agent 路由改造，能够避免在不稳定基线上同时处理架构和环境问题。

## 14. 最终展示建议

汇报时重点演示五条路径：

1. 正常路由：查询员工基础信息，主 Agent 精准进入 HR Agent。
2. 歧义澄清：用户只说“帮我安排一下”，主 Agent 不盲目派发。
3. 越权拒绝：普通员工查询薪资，被路由前权限闸门阻断。
4. 审批执行：发送外部邮件或提交差旅申请，审批后从检查点恢复。
5. 失败恢复：目标工具超时，主 Agent 在权限范围内重试或使用等价工具。

最终课题亮点应落在：

> 以强主 Agent 为中心，通过结构化任务理解、权限硬约束下的部门 Agent 精准路由、任务级最小授权和全链路闭环治理，使数字员工体系既能“调得准”，也能“管得住、追得清、可恢复”。
