# HR助理与文档生成功能使用说明

## 功能概述

新增了两个智能体，用于处理HR相关查询和文档生成：

1. **RemoteHRAssistantAgent**（人力资源助理）- 查询员工信息和工资数据
2. **RemoteDocumentGeneratorAgent**（文档生成器）- 生成Word格式的证明文档

## 架构设计

```
用户请求："帮我开买房用的个人收入证明"
    ↓
Planner规划：
  Step 1: RemoteHRAssistantAgent
          → 查询员工基本信息（remote_person_info_tool）
          → 查询员工工资信息（remote_salary_info_tool）
          → 输出：员工完整HR数据
    ↓
  Step 2: RemoteDocumentGeneratorAgent
          → 使用收入证明模板（remote_docx_generator_tool）
          → 填充员工数据
          → 输出：income_proof_张三_20260326.docx
```

## 1. RemoteHRAssistantAgent

### 功能
- 查询员工基本信息（姓名、职位、入职时间等）
- 查询员工工资信息（月收入、年收入、工资明细）

### 挂载工具
1. **remote_person_info_tool** - 查询人员信息
2. **remote_salary_info_tool** - 查询工资信息

### 数据源
- `assets/person_info_sample.json` - 员工基本信息
- `assets/mock_salary_db.json` - 员工工资数据

### 示例查询
```
"查询张三的工资信息"
"查询员工1234567的收入情况"
```

## 2. RemoteDocumentGeneratorAgent

### 功能
- 根据模板生成Word文档
- 支持多种证明类型（收入证明、在职证明等）
- 自动填充数据和格式化

### 挂载工具
- **remote_docx_generator_tool** - 生成Word文档

### 支持的模板
1. **income_proof** - 收入证明
2. **employment_certificate** - 在职证明
3. **recommendation_letter** - 推荐信（待扩展）

### 模板配置
- `assets/document_templates.json` - 模板定义文件

### 输出目录
- `output/` - 生成的文档保存在此目录

## 使用示例

### 示例1：开具收入证明
```
用户："帮我开买房用的个人收入证明"

系统执行：
1. RemoteHRAssistantAgent 查询当前用户的信息和工资
2. RemoteDocumentGeneratorAgent 生成收入证明文档

输出：
- income_proof_张三_20260326.docx
- 文件路径：output/income_proof_张三_20260326.docx
```

### 示例2：查询工资信息
```
用户："查询张三的工资情况"

系统执行：
1. RemoteHRAssistantAgent 查询工资信息

输出：
{
  "employee_name": "张三",
  "monthly_salary": 15000.00,
  "annual_salary": 180000.00,
  "salary_breakdown": {
    "base_salary": 10000.00,
    "performance_bonus": 3000.00,
    "position_allowance": 1500.00,
    "meal_allowance": 500.00
  }
}
```

## 数据格式

### 工资数据格式（mock_salary_db.json）
```json
{
  "salary_records": [
    {
      "employee_id": "1234567",
      "employee_name": "张三",
      "id_number": "110101198501011234",
      "monthly_salary": 15000.00,
      "annual_salary": 180000.00,
      "salary_breakdown": {
        "base_salary": 10000.00,
        "performance_bonus": 3000.00,
        "position_allowance": 1500.00,
        "meal_allowance": 500.00
      },
      "currency": "CNY",
      "last_updated": "2026-03-01"
    }
  ]
}
```

### 文档生成参数
```json
{
  "template_name": "income_proof",
  "data": {
    "name": "张三",
    "id_number": "110101198501011234",
    "position": "高级经理",
    "join_date": "2020年1月1日",
    "monthly_salary": "15000.00",
    "annual_salary": "180000.00"
  },
  "output_filename": "income_proof_张三_20260326"
}
```

## 技术实现

### 依赖库
- `python-docx` - Word文档生成

### 安装依赖
```bash
pip install python-docx
```

### 数字转中文
系统自动将金额转换为中文大写：
- 15000.00 → 壹万伍仟元整
- 180000.00 → 壹拾捌万元整

## 扩展指南

### 添加新模板
1. 在 `assets/document_templates.json` 中添加模板定义
2. 在 `mock_remote_registry.json` 的 `RemoteDocumentGeneratorAgent` 中添加模板名称到 enum
3. 重启服务

### 添加新员工数据
编辑 `assets/mock_salary_db.json`，添加新的 salary_records 条目

## 启动服务

```bash
# 1. 启动工具服务器
python mock_remote_tool_skill.py
# 监听端口: 8011

# 2. 启动远程Agent服务器
python mock_remote_agent.py
# 监听端口: 8010

# 3. 启动主服务
python cli.py web --host 0.0.0.0 --port 8001
```

## 注意事项

1. **数据安全**：工资数据为敏感信息，实际部署时需要加密存储和传输
2. **权限控制**：需要添加权限验证，确保只有授权用户可以查询工资和生成证明
3. **电子签章**：当前生成的文档只有"（盖章）"标记，实际使用需要集成电子签章系统
4. **模板管理**：建议将模板文件存储在数据库中，支持在线编辑和版本管理

## 改进建议

1. 支持PDF格式输出
2. 支持批量生成证明
3. 添加文档审批流程
4. 集成电子签章系统
5. 添加文档归档功能
