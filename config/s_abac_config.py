"""SuperAgent 的 S-ABAC 权限策略与属性配置。

S-ABAC 在这里表示 Scenario-aware Attribute-Based Access Control，即“场景感知的
属性访问控制”。权限判断不只看角色，还会同时看四类信息：

- Subject：谁在发起操作，例如某个 Agent 或系统编排器。
- Object：被访问的对象，例如工具、远程工具、被调度的 Agent。
- Scenario：当前任务场景，例如 workflow 阶段、风险等级、网络环境、时间。
- Action：要执行的动作，例如调度 Agent、调用工具、发送邮件。

第一版先把策略写在 Python 配置中，避免额外引入配置解析器。运行时资源 metadata
仍然可以补充或覆盖这里的 Object 属性。
"""

# 资源敏感度到数值等级的映射。
# PolicyEngine 会用它和 Subject 的 clearance_level 做比较：
# clearance_level >= sensitivity level 时，默认兜底规则才可能放行。
SENSITIVITY_LEVELS = {
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
    "CRITICAL": 4,
}

# 默认主体属性。
# 当某个 Agent 没有出现在 AGENT_SECURITY_ATTRIBUTES 中时，会使用这组属性。
# 这让未配置 Agent 仍然能参与权限判断，但权限等级保持相对保守。
DEFAULT_SUBJECT_ATTRIBUTES = {
    "role": "UniversalAssistant",
    "department": "General",
    "clearance_level": 2,
    "trust_level": "MEDIUM",
}

# 系统编排器的主体属性。
# agent_proxy_node 在调度目标 Agent 前，会以这个 Subject 身份检查
# “SuperAgent 是否允许调度某个 Agent”。
SYSTEM_SUBJECT_ATTRIBUTES = {
    "role": "UniversalAssistant",
    "department": "System",
    "clearance_level": 5,
    "trust_level": "HIGH",
}

# Agent 名称到安全主体属性的映射。
# SecurityContextBuilder 会根据 agent.agent_name 查这里，把业务 Agent 转换成
# S-ABAC 的 Subject。常用字段含义：
# - role：权限角色，用于匹配 allowed_roles 和显式策略。
# - department：所属业务域，便于后续扩展部门/组织边界策略。
# - clearance_level：权限等级，用于和资源敏感度比较。
# - trust_level：信任等级，当前主要作为审计和扩展字段保留。
AGENT_SECURITY_ATTRIBUTES = {
    "researcher": {
        "role": "ResearchAgent",
        "department": "Research",
        "clearance_level": 2,
        "trust_level": "MEDIUM",
    },
    "coder": {
        "role": "CodeAgent",
        "department": "Engineering",
        "clearance_level": 3,
        "trust_level": "HIGH",
    },
    "browser": {
        "role": "BrowserAgent",
        "department": "Research",
        "clearance_level": 2,
        "trust_level": "MEDIUM",
    },
    "reporter": {
        "role": "ReportAgent",
        "department": "General",
        "clearance_level": 2,
        "trust_level": "MEDIUM",
    },
    "RemoteHRAssistantAgent": {
        "role": "HRAgent",
        "department": "HR",
        "clearance_level": 3,
        "trust_level": "HIGH",
    },
    "RemoteOfficeAssistantAgent": {
        "role": "OperationAgent",
        "department": "Office",
        "clearance_level": 3,
        "trust_level": "HIGH",
    },
    "RemoteDocumentGeneratorAgent": {
        "role": "DocumentAgent",
        "department": "Office",
        "clearance_level": 3,
        "trust_level": "HIGH",
    },
    "RemoteEmailDispatchAgent": {
        "role": "CommunicationAgent",
        "department": "Office",
        "clearance_level": 3,
        "trust_level": "HIGH",
    },
    "RemoteCommunicationAgent": {
        "role": "CommunicationAgent",
        "department": "Office",
        "clearance_level": 3,
        "trust_level": "HIGH",
    },
    "RemoteKnowledgeAgent": {
        "role": "KnowledgeAgent",
        "department": "HR",
        "clearance_level": 2,
        "trust_level": "HIGH",
    },
    "RemoteBusinessRiskAgent": {
        "role": "RiskAgent",
        "department": "Risk",
        "clearance_level": 4,
        "trust_level": "HIGH",
    },
    "RemoteUnicornSelectorAgent": {
        "role": "ResearchAgent",
        "department": "Business",
        "clearance_level": 2,
        "trust_level": "MEDIUM",
    },
    "RemoteReportAgent": {
        "role": "ReportAgent",
        "department": "Business",
        "clearance_level": 2,
        "trust_level": "MEDIUM",
    },
    "RemoteMeetingManagerAgent": {
        "role": "OperationAgent",
        "department": "Office",
        "clearance_level": 3,
        "trust_level": "HIGH",
    },
}

# 默认客体属性。
# 当某个工具、远程资源或目标 Agent 没有出现在 RESOURCE_SECURITY_ATTRIBUTES 中时，
# 会使用这组默认属性。
DEFAULT_OBJECT_ATTRIBUTES = {
    "sensitivity": "LOW",
    "allowed_roles": [],
    "require_human_approval": False,
    "protocol": "local",
}

# 资源、工具、目标 Agent 到安全客体属性的映射。
# SecurityContextBuilder 会根据工具名、ResourceSpec.name 或目标 Agent 名称查这里，
# 把被访问对象转换成 S-ABAC 的 Object。
#
# 常用字段含义：
# - sensitivity：资源敏感度，取 LOW/MEDIUM/HIGH/CRITICAL。
# - allowed_roles：允许访问该资源的角色列表；为空表示不限制角色。
# - require_human_approval：即使角色和等级满足，也必须进入人工审批。
# - irreversible：操作不可逆，例如发送邮件、执行 shell、写入业务记录。
# - protocol/server_id/category 等字段可由运行时 metadata 补充，用于扩展策略。
RESOURCE_SECURITY_ATTRIBUTES = {
    # 低敏研究类工具：普通搜索/爬虫，ResearchAgent 和系统助手可直接使用。
    "tavily_search_results_json": {
        "sensitivity": "LOW",
        "allowed_roles": ["ResearchAgent", "UniversalAssistant"],
    },
    "crawl_tool": {
        "sensitivity": "LOW",
        "allowed_roles": ["ResearchAgent", "UniversalAssistant"],
    },
    # 本地代码执行类工具：比搜索更敏感，因此提升到 MEDIUM 或 HIGH。
    "python_repl": {
        "sensitivity": "MEDIUM",
        "allowed_roles": ["CodeAgent", "UniversalAssistant"],
    },
    "bash": {
        "sensitivity": "HIGH",
        "allowed_roles": ["CodeAgent"],
        "require_human_approval": True,
        "irreversible": True,
    },
    # 浏览器/文件写入类工具：可能访问外部页面或修改本地文件，设置为中敏。
    "browser": {
        "sensitivity": "MEDIUM",
        "allowed_roles": ["BrowserAgent", "ResearchAgent", "UniversalAssistant"],
    },
    "write_file": {
        "sensitivity": "MEDIUM",
        "allowed_roles": ["CodeAgent", "DocumentAgent", "UniversalAssistant"],
    },
    # HR 数据工具：人员、薪资信息是高敏资源，只允许 HRAgent。
    # 薪资信息额外要求人工审批。
    "remote_person_info_tool": {
        "sensitivity": "HIGH",
        "allowed_roles": ["HRAgent"],
    },
    "remote_salary_info_tool": {
        "sensitivity": "HIGH",
        "allowed_roles": ["HRAgent"],
        "require_human_approval": True,
    },
    # 文档生成工具：允许文档 Agent 或 HR Agent 使用。
    "remote_docx_generator_tool": {
        "sensitivity": "MEDIUM",
        "allowed_roles": ["DocumentAgent", "HRAgent"],
    },
    # 邮件发送工具：外发动作不可逆，因此高敏并要求人工审批。
    "remote_email_tool": {
        "sensitivity": "HIGH",
        "allowed_roles": ["CommunicationAgent"],
        "require_human_approval": True,
        "irreversible": True,
    },
    # 知识检索工具：通常读内部知识库，按中敏处理。
    "knowledge_search_tool": {
        "sensitivity": "MEDIUM",
        "allowed_roles": ["KnowledgeAgent", "HRAgent", "UniversalAssistant"],
    },
    # 业务写入类工具：请假、差旅等会写业务记录，因此高敏并进入审批。
    "save_leave_record": {
        "sensitivity": "HIGH",
        "allowed_roles": ["OperationAgent", "HRAgent"],
        "require_human_approval": True,
    },
    "save_travel_record": {
        "sensitivity": "HIGH",
        "allowed_roles": ["OperationAgent", "HRAgent"],
        "require_human_approval": True,
    },
    # 目标 Agent 本身也可以作为 Object。
    # agent_proxy_node 调度这些 Agent 前，会检查调度权限。
    "RemoteHRAssistantAgent": {
        "sensitivity": "HIGH",
        "allowed_roles": ["UniversalAssistant"],
    },
    "RemoteDocumentGeneratorAgent": {
        "sensitivity": "MEDIUM",
        "allowed_roles": ["UniversalAssistant"],
    },
    "RemoteEmailDispatchAgent": {
        "sensitivity": "HIGH",
        "allowed_roles": ["UniversalAssistant"],
        "require_human_approval": True,
        "irreversible": True,
    },
}

# 显式 S-ABAC 策略列表。
# PolicyEngine 会先按顺序匹配这里的策略；如果没有命中，才进入默认兜底规则。
#
# 策略结构说明：
# - policy_id：策略唯一标识，便于审计和排查。
# - description：策略说明。
# - rules：该策略下的规则列表。
# - condition.all：所有条件都满足才命中。
# - condition.any：任意条件满足即可命中，目前此文件暂未使用。
# - effect：ALLOW 表示满足条件后允许进入约束检查；未允许则默认拒绝。
# - human_review_required：命中后强制进入人工审批。
# - constraints：额外约束，例如 allowed_actions、金额阈值、工作时间要求等。
S_ABAC_POLICIES = [
    {
        "policy_id": "P-SYSTEM-ORCHESTRATE-AGENTS",
        "description": "The SuperAgent orchestrator can delegate to registered agents.",
        "rules": [
            {
                # 系统编排器可以调度已注册 Agent。
                # 若目标 Agent 的 Object 属性要求人工审批，仍会在命中后进入审批。
                "condition": {
                    "all": [
                        {"subject.attributes.role": "UniversalAssistant"},
                        {"action.verb": "orchestrate"},
                        {"object.attributes.type": "agent"},
                    ]
                },
                "effect": "ALLOW",
                "constraints": {
                    "allowed_actions": ["delegate"],
                },
            }
        ],
    },
    {
        "policy_id": "P-HR-SENSITIVE-TOOLS",
        "description": "HR agents can use HR sensitive tools, with review when configured.",
        "rules": [
            {
                # HR Agent 可以调用 HR 分类工具。
                # 具体是否还要人审，由工具 Object 的 require_human_approval 等属性决定。
                "condition": {
                    "all": [
                        {"subject.attributes.role": "HRAgent"},
                        {"action.verb": "execute"},
                        {"object.attributes.category": "HR"},
                    ]
                },
                "effect": "ALLOW",
                "constraints": {
                    "allowed_actions": ["call", "query", "execute"],
                    "require_working_hours": False,
                },
            }
        ],
    },
    {
        "policy_id": "P-COMMUNICATION-SEND",
        "description": "Communication agents can send messages and emails with review.",
        "rules": [
            {
                # 通信类 Agent 可以执行通信类工具，但发送消息/邮件属于外发动作，
                # 这里强制 human_review_required=True。
                "condition": {
                    "all": [
                        {"subject.attributes.role": "CommunicationAgent"},
                        {"action.verb": "execute"},
                        {"object.attributes.category": "Communication"},
                    ]
                },
                "effect": "ALLOW",
                "human_review_required": True,
                "constraints": {
                    "allowed_actions": ["call", "execute"],
                },
            }
        ],
    },
]
