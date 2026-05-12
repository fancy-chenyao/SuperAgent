# Agent 与工具清单

本文档列出了系统中所有 Agent 和工具的定义及其安全属性。

---

## 1. Agent 清单

| Agent 名称 | 角色 (Role) | 部门 | 权限等级 (CL) | 信任等级 |
|---|---|---|---|---|
| `researcher` | ResearchAgent | Research | 2 | MEDIUM |
| `coder` | CodeAgent | Engineering | 3 | HIGH |
| `browser` | BrowserAgent | Research | 2 | MEDIUM |
| `reporter` | ReportAgent | General | 2 | MEDIUM |
| `RemoteHRAssistantAgent` | HRAgent | HR | 3 | HIGH |
| `RemoteOfficeAssistantAgent` | OperationAgent | Office | 3 | HIGH |
| `RemoteDocumentGeneratorAgent` | DocumentAgent | Office | 3 | HIGH |
| `RemoteEmailDispatchAgent` | CommunicationAgent | Office | 3 | HIGH |
| `RemoteCommunicationAgent` | CommunicationAgent | Office | 3 | HIGH |
| `RemoteKnowledgeAgent` | KnowledgeAgent | HR | 2 | HIGH |
| `RemoteBusinessRiskAgent` | RiskAgent | Risk | 4 | HIGH |
| `RemoteUnicornSelectorAgent` | ResearchAgent | Business | 2 | MEDIUM |
| `RemoteReportAgent` | ReportAgent | Business | 2 | MEDIUM |
| `RemoteMeetingManagerAgent` | OperationAgent | Office | 3 | HIGH |

### Agent 角色说明

| 角色 | 说明 |
|---|---|
| `ResearchAgent` | 信息检索、搜索、爬虫、数据分析 |
| `CodeAgent` | 代码编写、执行、文件操作 |
| `BrowserAgent` | 浏览器操作、网页交互 |
| `ReportAgent` | 报告生成、文档整理 |
| `HRAgent` | HR 相关业务（人员信息、薪资等） |
| `OperationAgent` | 运营相关操作（会议管理、办公辅助） |
| `DocumentAgent` | 文档生成（Word、PDF 等） |
| `CommunicationAgent` | 邮件发送、消息通知 |
| `KnowledgeAgent` | 内部知识库检索 |
| `RiskAgent` | 业务风险分析、合规评估 |

---

## 2. 工具清单

### 2.1 本地工具

| 工具名称 | 敏感度 | 允许角色 |
|---|---|---|
| `tavily_search_results_json` | LOW | ResearchAgent, UniversalAssistant |
| `crawl_tool` | LOW | ResearchAgent, UniversalAssistant |
| `python_repl` | MEDIUM | CodeAgent, UniversalAssistant |
| `bash` | HIGH | CodeAgent, UniversalAssistant |
| `browser` | MEDIUM | BrowserAgent, ResearchAgent, UniversalAssistant |
| `write_file` | MEDIUM | CodeAgent, DocumentAgent, UniversalAssistant |

### 2.2 远程工具

| 工具名称 | 敏感度 | 允许角色 |
|---|---|---|
| `remote_person_info_tool` | HIGH | HRAgent, UniversalAssistant |
| `remote_salary_info_tool` | HIGH | HRAgent, UniversalAssistant |
| `remote_docx_generator_tool` | MEDIUM | DocumentAgent, HRAgent, UniversalAssistant |
| `remote_email_tool` | HIGH | CommunicationAgent, UniversalAssistant |
| `knowledge_search_tool` | MEDIUM | KnowledgeAgent, HRAgent, UniversalAssistant |
| `save_leave_record` | HIGH | OperationAgent, HRAgent, UniversalAssistant |
| `save_travel_record` | HIGH | OperationAgent, HRAgent, UniversalAssistant |

### 2.3 工具作为可调度对象

以下 Agent 也可作为工具被其他 Agent 调度，其调度权限配置如下：

| Agent 名称 | 敏感度 | 允许调度角色 |
|---|---|---|
| `RemoteHRAssistantAgent` | HIGH | UniversalAssistant, HRAgent |
| `RemoteDocumentGeneratorAgent` | MEDIUM | UniversalAssistant, HRAgent, DocumentAgent, CommunicationAgent |
| `RemoteEmailDispatchAgent` | HIGH | UniversalAssistant, CommunicationAgent |
| `RemoteCommunicationAgent` | MEDIUM | UniversalAssistant, CommunicationAgent |
| `RemoteKnowledgeAgent` | LOW | UniversalAssistant, KnowledgeAgent, HRAgent, ResearchAgent |
| `RemoteBusinessRiskAgent` | HIGH | UniversalAssistant, RiskAgent |
| `RemoteUnicornSelectorAgent` | LOW | UniversalAssistant, ResearchAgent |
| `RemoteReportAgent` | LOW | UniversalAssistant, ReportAgent |
| `RemoteMeetingManagerAgent` | MEDIUM | UniversalAssistant, OperationAgent |
| `RemoteOfficeAssistantAgent` | MEDIUM | UniversalAssistant, OperationAgent, CommunicationAgent |
| `researcher` | LOW | UniversalAssistant, ResearchAgent, HRAgent, CodeAgent, CommunicationAgent |
| `coder` | MEDIUM | UniversalAssistant, CodeAgent |
| `browser` | LOW | UniversalAssistant, BrowserAgent, ResearchAgent, CodeAgent |
| `reporter` | LOW | UniversalAssistant, ReportAgent, ResearchAgent, HRAgent, CodeAgent, CommunicationAgent |

---

## 3. 敏感度等级

| 等级 | 数值 | 说明 |
|---|---|---|
| LOW | 1 | 公开或低敏感数据，如搜索结果 |
| MEDIUM | 2 | 中等敏感数据，如代码执行、文档生成 |
| HIGH | 3 | 高敏感数据，如薪资、邮件、业务记录 |
| CRITICAL | 4 | 最高敏感级别 |

**权限等级与敏感度关系**：Subject 的 `clearance_level` 必须 >= Object 的敏感度数值，才可能通过默认兜底规则。

---

## 4. 用户角色映射

| 用户 | 角色 | 权限等级 (CL) | 可用 Agent |
|---|---|---|---|
| `admin` | UniversalAssistant | 5 | 全部 |
| `hr_manager` | HRAgent | 3 | RemoteHRAssistantAgent, RemoteDocumentGeneratorAgent, RemoteKnowledgeAgent, reporter, researcher |
| `engineer` | CodeAgent | 3 | coder, researcher, browser, reporter |
| `researcher_user` | ResearchAgent | 2 | researcher, browser, reporter |
| `guest` | UniversalAssistant | 1 | researcher |
| `communication_officer` | CommunicationAgent | 3 | RemoteCommunicationAgent, RemoteEmailDispatchAgent, RemoteDocumentGeneratorAgent, RemoteOfficeAssistantAgent, researcher, reporter |

---

## 5. 显式策略

| 策略 ID | 说明 | 条件 |
|---|---|---|
| `P-SYSTEM-ORCHESTRATE-AGENTS` | 系统编排器可调度所有 Agent | subject_type=system, action=orchestrate |
| `P-HR-SENSITIVE-TOOLS` | HR Agent 可使用 HR 敏感工具 | role=HRAgent, category=HR |
| `P-COMMUNICATION-SEND` | Communication Agent 可发送邮件消息 | role=CommunicationAgent, category=Communication |
