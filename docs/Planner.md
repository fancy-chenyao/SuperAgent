# 分层规划架构方案

## 一、问题背景

当前系统采用中心化规划方式，在智能体数量较少（<10个）且工具量有限时表现良好。但在生产环境中面临以下挑战：

- **规模瓶颈**：成百上千个智能体时，单次规划需要处理海量信息
- **效率问题**：中心规划器需要理解所有智能体和工具，规划时间随规模线性增长
- **准确性下降**：信息过载导致规划质量不稳定

## 二、解决方案

采用 **智能体聚类 + 专家路由 + 基于检索的规划 + 分层规划** 的混合架构：

```
用户需求
    ↓
[元规划器] ← 检索历史成功计划
    ↓
高层任务分解（按领域）
    ↓
[领域专家路由] ← 智能体聚类
    ↓
并行领域规划
    ↓
详细执行步骤
```

### 核心组件

#### 1. 智能体聚类（分层规划基础）

按业务领域对智能体进行聚类，每个领域包含相关智能体和工具：

```python
# 领域定义
class AgentDomain(Enum):
    DATA_PROCESSING = "data_processing"      # 数据处理
    WEB_RESEARCH = "web_research"            # 网络研究
    CODE_GENERATION = "code_generation"      # 代码生成
    DOCUMENT_ANALYSIS = "document_analysis"  # 文档分析
    API_INTEGRATION = "api_integration"      # API集成
    DATABASE_OPS = "database_ops"            # 数据库操作
    TESTING_QA = "testing_qa"                # 测试质量保证
    DEPLOYMENT = "deployment"                # 部署运维

# 聚类结构
class DomainCluster:
    domain: AgentDomain
    agents: List[str]           # 该领域的智能体列表
    tools: List[str]            # 该领域的工具列表
    description: str            # 领域描述
    embedding: Optional[List[float]]  # 领域语义向量
```

**优势**：
- 将 O(N个智能体) 的复杂度降低到 O(M个领域)，M << N
- 新增智能体只需加入对应领域，无需修改全局规划逻辑
- 领域内专家更精准理解任务需求

#### 2. 基于检索的规划

使用向量数据库存储历史成功的执行计划：

```python
class HistoricalPlan:
    plan_id: str
    user_query: str                    # 原始用户需求
    query_embedding: List[float]       # 需求的向量表示
    high_level_plan: List[dict]        # 高层任务分解
    detailed_steps: List[dict]         # 详细执行步骤
    execution_result: str              # 执行结果（成功/失败）
    success_score: float               # 成功评分 0-1
    created_at: datetime
    metadata: dict                     # 额外元数据

class PlanRetriever:
    """计划检索器"""

    async def retrieve_similar_plans(
        self,
        query: str,
        query_embedding: List[float],
        top_k: int = 3
    ) -> List[HistoricalPlan]:
        """检索相似的历史计划"""
        # 1. 向量相似度搜索
        # 2. 过滤低质量计划（success_score < 0.7）
        # 3. 返回 top_k 个最相似的成功计划
        pass

    async def store_plan(
        self,
        plan: HistoricalPlan
    ):
        """存储执行计划"""
        # 1. 生成 embedding
        # 2. 存入向量数据库
        # 3. 关联执行结果
        pass
```

**优势**：
- 重复性任务直接复用历史计划，无需重新规划
- 为新任务提供参考案例，提升规划质量
- 随着使用积累，系统越来越智能

【补全建议】检索质量闭环必须明确，否则检索会逐步污染。
默认实现策略：
- `success_score` 明确定义（执行状态 + 完成率 + 可选用户评分）
- 检索时应用时间衰减（例如 30 天半衰期）
- 相似计划去重（高相似度只保留最新）
- 失败计划进入负样本库（用于对比或过滤）

#### 3. 分层规划流程

**第一层：元规划（Meta Planning）**

```python
class MetaPlanner:
    """元规划器：生成高层任务分解"""

    async def generate_high_level_plan(
        self,
        user_query: str,
        state: dict
    ) -> List[HighLevelTask]:
        """
        输入：用户需求
        输出：2-5个高层任务，每个任务分配到一个领域

        步骤：
        1. 检索相似历史计划（基于向量相似度）
        2. 调用 LLM 分解任务
        3. 为每个任务分配领域
        4. 标注任务依赖关系
        """
        pass

class HighLevelTask:
    task_id: str
    description: str              # 任务描述
    domain: AgentDomain          # 所属领域
    dependencies: List[str]      # 依赖的任务ID
    estimated_complexity: str    # 复杂度估计
```

**第二层：领域专家规划（Domain Expert Planning）**

```python
class DomainExpertPlanner:
    """领域专家规划器：为特定领域生成详细执行步骤"""

    def __init__(self, domain: AgentDomain, cluster: DomainCluster):
        self.domain = domain
        self.cluster = cluster  # 该领域的智能体和工具

    async def plan_task(
        self,
        task: HighLevelTask,
        context: dict
    ) -> List[DetailedStep]:
        """
        输入：高层任务 + 上下文
        输出：2-5个详细执行步骤

        特点：
        - 只关注本领域的智能体和工具
        - 可以并行执行（不同领域的专家同时规划）
        - 专家更精准理解领域内任务
        """
        pass

class DetailedStep:
    step_id: str
    agent_name: str              # 使用的智能体
    description: str             # 步骤描述
    tools: List[str]             # 使用的工具
    expected_output: str         # 预期输出
```

#### 4. 专家路由

```python
class ExpertRouter:
    """专家路由器：将任务路由到合适的领域专家"""

    def __init__(self, clustering_manager: AgentClusteringManager):
        self.clustering = clustering_manager

    async def route_task(
        self,
        task: HighLevelTask
    ) -> DomainExpertPlanner:
        """
        路由策略：
        1. 基于任务的 domain 字段直接路由
        2. 如果 domain 未指定，使用 embedding 相似度匹配
        3. 支持多领域任务（返回多个专家）
        """
        cluster = self.clustering.get_cluster(task.domain)
        return DomainExpertPlanner(task.domain, cluster)
```

【补全建议】路由置信度与回退机制是可用性的关键。
默认实现策略：
- 置信度阈值 `0.75`
- 低置信度任务走多领域并行（top-2）再合并
- 若低置信度任务占比 >50%，整体回退中心化规划

## 三、实现架构

### 目录结构

```
src/workflow/planning/
├── __init__.py
├── hierarchical_planner.py      # 分层规划核心逻辑
├── meta_planner.py              # 元规划器
├── domain_expert.py             # 领域专家规划器
├── plan_retriever.py            # 计划检索器
└── hierarchical_planner_node.py # LangGraph 节点

src/manager/
├── clustering_manager.py        # 智能体聚类管理
└── domain_classifier.py         # 领域分类器

config/
└── planning_config.py           # 规划配置
```

### 核心代码示例

```python
# hierarchical_planner_node.py
async def hierarchical_planner_node(state: State) -> Command:
    """分层规划节点：替代原有的 planner_node"""

    # === 阶段 1: 元规划 ===
    meta_planner = MetaPlanner(
        llm=get_llm_by_type("planner"),
        plan_retriever=plan_retriever,
        clustering_manager=clustering_manager
    )

    high_level_tasks = await meta_planner.generate_high_level_plan(
        user_query=user_query,
        state=state
    )

    # === 阶段 2: 领域专家并行规划 ===
    expert_planning_tasks = []
    for task in high_level_tasks:
        cluster = clustering_manager.get_cluster(task.domain)
        expert_planner = DomainExpertPlanner(task.domain, cluster, llm)
        expert_planning_tasks.append(
            expert_planner.plan_task(task, context=state)
        )

    # 并行执行所有领域专家规划
    detailed_steps_list = await asyncio.gather(*expert_planning_tasks)

    # === 阶段 3: 合并计划 ===
    final_plan = merge_plans(high_level_tasks, detailed_steps_list)

    # 存储成功计划供未来检索
    await plan_retriever.store_plan(final_plan)

    return Command(update={"messages": [...]}, goto="publisher")
```

【补全建议】并行规划必须有超时与降级策略。
默认实现策略：
- 单专家超时 30s，总规划超时 60s
- 超时任务回退为简化计划
- `gather(return_exceptions=True)` 处理异常并降级

## 四、性能对比

### 中心化规划 vs 分层规划

| 指标 | 中心化规划 | 分层规划 |
|------|-----------|---------|
| **规划时间** | O(N个智能体) | O(M个领域) + 并行 |
| **10个智能体** | ~5秒 | ~3秒 |
| **100个智能体** | ~30秒 | ~5秒 |
| **1000个智能体** | >120秒 | ~8秒 |
| **准确性** | 信息过载，质量下降 | 领域专家，质量稳定 |
| **可扩展性** | 线性增长 | 对数增长 |
| **重复任务** | 每次重新规划 | 检索复用，<1秒 |

### 实际案例

**任务**：分析100个PDF文档，提取关键信息，生成报告

**中心化规划**：
- 规划时间：25秒
- 需要理解所有智能体（文档分析、数据处理、报告生成等）
- 单次 LLM 调用处理所有信息

**分层规划**：
- 元规划：3秒（分解为3个高层任务）
  - 任务1：文档分析（document_analysis 领域）
  - 任务2：数据聚合（data_processing 领域）
  - 任务3：报告生成（code_generation 领域）
- 领域专家规划：并行执行，共4秒
- 总时间：7秒（提升 **71%**）

## 五、实施路径

### 阶段 1: 基础设施（1-2周）

1. **向量数据库集成**
   - 选择：Chroma（开发）/ Qdrant（生产）
   - 实现 `PlanRetriever` 类
   - 添加计划存储/检索接口

2. **智能体聚类**
   - 实现 `AgentClusteringManager`
   - 定义默认8个领域分类
   - 支持动态添加新领域

### 阶段 2: 分层规划（2-3周）

1. **元规划器**
   - 实现 `MetaPlanner` 类
   - 集成历史计划检索
   - 优化提示词模板

2. **领域专家规划器**
   - 为每个领域实现 `DomainExpertPlanner`
   - 支持并行规划
   - 添加计划合并逻辑

3. **替换现有节点**
   - 实现 `hierarchical_planner_node`
   - 保持向后兼容
   - 添加性能监控

### 阶段 3: 优化与扩展（持续）

1. **计划质量提升**
   - 收集执行反馈
   - 自动标注成功/失败计划
   - 定期清理低质量计划

2. **智能路由优化**
   - 使用 embedding 替代关键词匹配
   - 支持多领域任务
   - 动态调整领域分类

3. **性能优化**
   - 计划缓存策略
   - 异步并行优化
   - 减少 LLM 调用次数

## 六、配置示例

```python
# config/planning_config.py
PLANNING_CONFIG = {
    "meta_planner": {
        "model": "planner",
        "max_high_level_tasks": 5,
        "retrieval_top_k": 3,
        "similarity_threshold": 0.75
    },
    "domain_expert": {
        "model": "planner",
        "max_steps_per_task": 5,
        "parallel_planning": True,
        "timeout_seconds": 30
    },
    "vector_db": {
        "type": "chroma",  # or "qdrant"
        "persist_directory": "./store/plan_vectors",
        "collection_name": "historical_plans",
        "embedding_model": "text-embedding-3-small"
    },
    "clustering": {
        "auto_classify": True,
        "fallback_domain": "data_processing",
        "domains": {
            "data_processing": {
                "agents": ["data_analyst", "csv_processor"],
                "tools": ["pandas_tool", "numpy_tool"],
                "description": "数据清洗、转换、分析"
            },
            "web_research": {
                "agents": ["researcher", "web_scraper"],
                "tools": ["tavily_tool", "selenium_tool"],
                "description": "网络搜索、信息收集"
            }
            # ... 其他领域
        }
    },
    "plan_storage": {
        "auto_save": True,
        "min_success_score": 0.7,  # 只存储成功率>70%的计划
        "retention_days": 90
    }
}
```

## 七、关键优势

### 1. 可扩展性
- 从10个智能体扩展到1000+个
- 元规划复杂度：O(领域数) 而非 O(智能体数)
- 领域内规划并行执行

### 2. 准确性
- 领域专家更精准理解任务
- 历史计划提供参考案例
- 减少信息过载导致的规划错误

### 3. 性能
- 并行规划节省时间（3-5倍提升）
- 检索复用减少 LLM 调用（重复任务<1秒）
- 分层降低单次规划复杂度

### 4. 可维护性
- 领域清晰分离，职责明确
- 易于添加新智能体（只需加入对应领域）
- 计划可追溯和优化

## 八、监控指标

```python
# 规划性能监控
class PlanningMetrics:
    meta_planning_time: float      # 元规划耗时
    expert_planning_time: float    # 专家规划耗时
    total_planning_time: float     # 总规划耗时
    num_high_level_tasks: int      # 高层任务数
    num_detailed_steps: int        # 详细步骤数
    cache_hit: bool                # 是否命中缓存
    retrieval_similarity: float    # 检索相似度
    domains_involved: List[str]    # 涉及的领域
```

【补全建议】需要离线评测集 + 在线监控指标，才能验证“提升幅度”。
默认实现策略：
- 离线：10-20 个固定任务用例评测（时间、步骤数、领域覆盖）
- 在线：P50/P95 规划时间、成功率、回退率、缓存命中率

## 九、迁移指南

### 从现有 planner_node 迁移

1. **保持兼容**：新节点输出格式与原节点一致
2. **渐进式切换**：通过配置开关控制使用新/旧规划器
3. **A/B测试**：对比两种方案的性能和准确性

```python
# 配置开关
USE_HIERARCHICAL_PLANNER = os.getenv("USE_HIERARCHICAL_PLANNER", "false") == "true"

if USE_HIERARCHICAL_PLANNER:
    planner_node = hierarchical_planner_node
else:
    planner_node = original_planner_node
```

【补全建议】A/B 迁移期间要记录双路径结果差异并做灰度回滚。
默认实现策略：
- 5% 灰度 + 15% 扩大 + 50% 扩大 + 100% 全量
- 任一阶段 P95 规划时间或成功率退化 >10% 立即回滚

## 十、关键风险与缓解策略

### 风险 1: 领域聚类和路由质量不稳定

**问题**：如果领域分类不准确，分层规划反而会降低质量

**缓解策略**：

```python
class DomainRouter:
    """带置信度的领域路由器"""

    CONFIDENCE_THRESHOLD = 0.75  # 置信度阈值

    async def route_with_confidence(
        self,
        task: HighLevelTask
    ) -> Tuple[AgentDomain, float]:
        """返回领域和置信度"""

        # 1. 使用 embedding 计算与各领域的相似度
        task_embedding = await self._get_embedding(task.description)
        similarities = {}

        for domain, cluster in self.clustering.clusters.items():
            if cluster.embedding:
                similarity = cosine_similarity(task_embedding, cluster.embedding)
                similarities[domain] = similarity

        # 2. 获取最高置信度
        best_domain = max(similarities, key=similarities.get)
        confidence = similarities[best_domain]

        return best_domain, confidence

async def hierarchical_planner_node_with_fallback(state: State) -> Command:
    """带回退机制的分层规划"""

    # 元规划
    high_level_tasks = await meta_planner.generate_high_level_plan(...)

    # 检查路由置信度
    low_confidence_tasks = []
    for task in high_level_tasks:
        domain, confidence = await router.route_with_confidence(task)
        task.domain = domain
        task.routing_confidence = confidence

        if confidence < DomainRouter.CONFIDENCE_THRESHOLD:
            low_confidence_tasks.append(task)

    # 策略 1: 置信度不足时回退到中心化规划
    if len(low_confidence_tasks) > len(high_level_tasks) * 0.5:
        logger.warning(f"超过50%任务路由置信度不足，回退到中心化规划")
        return await original_planner_node(state)

    # 策略 2: 低置信度任务使用多领域并行规划
    for task in low_confidence_tasks:
        # 选择 top-2 领域并行规划，后续合并
        top_domains = sorted(
            similarities.items(),
            key=lambda x: x[1],
            reverse=True
        )[:2]
        task.candidate_domains = [d[0] for d in top_domains]

    # 继续分层规划...
```

**配置**：

```python
PLANNING_CONFIG = {
    "routing": {
        "confidence_threshold": 0.75,
        "fallback_to_centralized": True,  # 低置信度时回退
        "multi_domain_planning": True,    # 支持多领域并行
        "max_candidate_domains": 2
    }
}
```

### 风险 2: 计划检索质量不闭环

**问题**：success_score 定义不明确，无法有效过滤低质量计划

**缓解策略**：

```python
class ExecutionFeedback(BaseModel):
    """执行反馈"""
    plan_id: str
    execution_status: Literal["success", "partial_success", "failure"]
    steps_completed: int
    steps_total: int
    error_messages: List[str]
    execution_time: float
    user_satisfaction: Optional[int]  # 1-5 评分，可选

class PlanQualityEvaluator:
    """计划质量评估器"""

    def calculate_success_score(
        self,
        feedback: ExecutionFeedback
    ) -> float:
        """
        计算成功评分 (0-1)

        评分规则：
        - 执行状态：success=1.0, partial_success=0.6, failure=0.0
        - 完成率：steps_completed / steps_total
        - 用户满意度：user_satisfaction / 5 (如果有)
        """
        # 基础分：执行状态
        status_scores = {
            "success": 1.0,
            "partial_success": 0.6,
            "failure": 0.0
        }
        base_score = status_scores[feedback.execution_status]

        # 完成率加权
        completion_rate = feedback.steps_completed / feedback.steps_total

        # 综合评分
        score = base_score * 0.6 + completion_rate * 0.4

        # 用户满意度调整（如果有）
        if feedback.user_satisfaction:
            user_score = feedback.user_satisfaction / 5.0
            score = score * 0.7 + user_score * 0.3

        return score

class PlanRetriever:
    """增强的计划检索器"""

    async def store_plan_with_feedback(
        self,
        plan: HistoricalPlan,
        feedback: ExecutionFeedback
    ):
        """存储计划并关联执行反馈"""

        # 计算成功评分
        evaluator = PlanQualityEvaluator()
        plan.success_score = evaluator.calculate_success_score(feedback)
        plan.execution_result = feedback.execution_status

        # 只存储高质量计划
        if plan.success_score >= 0.7:
            await self.vector_db.add(...)
            logger.info(f"存储成功计划 {plan.plan_id}, 评分: {plan.success_score}")
        else:
            # 失败计划存入负样本库（用于对比学习）
            await self.store_negative_sample(plan, feedback)
            logger.info(f"计划 {plan.plan_id} 质量不足 ({plan.success_score}), 存入负样本")

    async def retrieve_similar_plans(
        self,
        query: str,
        query_embedding: List[float],
        top_k: int = 3
    ) -> List[HistoricalPlan]:
        """检索时应用时间衰减和去重"""

        # 1. 向量检索
        results = await self.vector_db.query(
            query_embeddings=[query_embedding],
            n_results=top_k * 2,  # 多检索一些用于过滤
            where={
                "execution_result": "success",
                "success_score": {"$gte": 0.7}
            }
        )

        # 2. 时间衰减（越新的计划权重越高）
        now = datetime.now()
        plans_with_score = []
        for plan in results:
            age_days = (now - plan.created_at).days
            time_decay = math.exp(-age_days / 30)  # 30天半衰期
            adjusted_score = plan.success_score * time_decay
            plans_with_score.append((plan, adjusted_score))

        # 3. 去重（相似度 > 0.95 的计划只保留最新的）
        deduplicated = self._deduplicate_plans(plans_with_score)

        # 4. 返回 top_k
        deduplicated.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in deduplicated[:top_k]]
```

**执行反馈采集时机**：

```python
# 在 workflow 执行完成后采集反馈
async def workflow_completion_handler(workflow_id: str, state: State):
    """工作流完成处理"""

    # 收集执行反馈
    feedback = ExecutionFeedback(
        plan_id=state["plan_id"],
        execution_status="success" if state["status"] == "completed" else "failure",
        steps_completed=state["steps_completed"],
        steps_total=state["steps_total"],
        error_messages=state.get("errors", []),
        execution_time=state["execution_time"]
    )

    # 存储计划和反馈
    await plan_retriever.store_plan_with_feedback(
        plan=state["original_plan"],
        feedback=feedback
    )
```

### 风险 3: 计划合并规则缺失

**问题**：merge_plans 的冲突处理、依赖排序、上下文裁剪未定义

**缓解策略**：

```python
class PlanMerger:
    """计划合并器"""

    def merge_plans(
        self,
        high_level_tasks: List[HighLevelTask],
        detailed_steps_list: List[List[DetailedStep]]
    ) -> List[DetailedStep]:
        """
        合并计划，处理依赖和冲突

        规则：
        1. 拓扑排序：按依赖关系排序任务
        2. 资源冲突检测：同一资源的操作串行化
        3. 上下文裁剪：限制传递的上下文大小
        """

        # 1. 构建任务依赖图
        task_graph = self._build_dependency_graph(high_level_tasks)

        # 2. 拓扑排序
        sorted_tasks = self._topological_sort(task_graph)

        # 3. 按顺序合并步骤
        merged_steps = []
        resource_locks = {}  # 资源锁：{resource_id: last_step_id}

        for task in sorted_tasks:
            steps = detailed_steps_list[high_level_tasks.index(task)]

            for step in steps:
                # 检测资源冲突
                conflicts = self._detect_resource_conflicts(step, resource_locks)

                if conflicts:
                    # 添加依赖，确保串行执行
                    step.dependencies.extend(conflicts)

                # 上下文裁剪
                step.context = self._trim_context(step.context, max_tokens=2000)

                # 更新资源锁
                for resource in step.resources:
                    resource_locks[resource] = step.step_id

                merged_steps.append(step)

        return merged_steps

    def _build_dependency_graph(
        self,
        tasks: List[HighLevelTask]
    ) -> Dict[str, List[str]]:
        """构建依赖图"""
        graph = {task.task_id: task.dependencies for task in tasks}
        return graph

    def _topological_sort(
        self,
        graph: Dict[str, List[str]]
    ) -> List[str]:
        """拓扑排序"""
        from collections import deque

        # 计算入度
        in_degree = {node: 0 for node in graph}
        for node in graph:
            for neighbor in graph[node]:
                in_degree[neighbor] += 1

        # BFS
        queue = deque([node for node in in_degree if in_degree[node] == 0])
        sorted_nodes = []

        while queue:
            node = queue.popleft()
            sorted_nodes.append(node)

            for neighbor in graph[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # 检测循环依赖
        if len(sorted_nodes) != len(graph):
            raise ValueError("检测到循环依赖")

        return sorted_nodes

    def _detect_resource_conflicts(
        self,
        step: DetailedStep,
        resource_locks: Dict[str, str]
    ) -> List[str]:
        """检测资源冲突"""
        conflicts = []

        # 定义资源冲突规则
        CONFLICT_RULES = {
            "file_write": ["file_write", "file_read"],  # 写文件与读写互斥
            "database_write": ["database_write"],       # 数据库写互斥
            "api_call": []                              # API调用无冲突
        }

        for resource in step.resources:
            resource_type = resource.split(":")[0]

            if resource in resource_locks:
                # 检查是否冲突
                last_step = resource_locks[resource]
                if resource_type in CONFLICT_RULES:
                    conflicts.append(last_step)

        return conflicts

    def _trim_context(
        self,
        context: dict,
        max_tokens: int = 2000
    ) -> dict:
        """裁剪上下文"""
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        context_str = json.dumps(context, ensure_ascii=False)
        tokens = enc.encode(context_str)

        if len(tokens) <= max_tokens:
            return context

        # 裁剪策略：保留关键字段
        trimmed = {
            "task_id": context.get("task_id"),
            "user_query": context.get("user_query"),
            "previous_outputs": context.get("previous_outputs", [])[-3:]  # 只保留最近3个输出
        }

        return trimmed
```

### 风险 4: 并行规划的预算与超时控制

**问题**：asyncio.gather 无超时控制，少数慢任务拖累全局

**缓解策略**：

```python
async def hierarchical_planner_node_with_timeout(state: State) -> Command:
    """带超时控制的分层规划"""

    # 配置
    EXPERT_PLANNING_TIMEOUT = 30  # 单个专家规划超时 30 秒
    TOTAL_PLANNING_TIMEOUT = 60   # 总规划超时 60 秒

    try:
        # 阶段 1: 元规划（带超时）
        high_level_tasks = await asyncio.wait_for(
            meta_planner.generate_high_level_plan(...),
            timeout=20
        )

        # 阶段 2: 领域专家并行规划（带超时和取消）
        expert_tasks = []
        for task in high_level_tasks:
            expert_planner = DomainExpertPlanner(...)
            expert_tasks.append(
                asyncio.wait_for(
                    expert_planner.plan_task(task, context=state),
                    timeout=EXPERT_PLANNING_TIMEOUT
                )
            )

        # 并行执行，带总超时
        detailed_steps_list = await asyncio.wait_for(
            asyncio.gather(*expert_tasks, return_exceptions=True),
            timeout=TOTAL_PLANNING_TIMEOUT
        )

        # 处理超时和异常
        valid_steps = []
        for i, result in enumerate(detailed_steps_list):
            if isinstance(result, asyncio.TimeoutError):
                logger.warning(f"任务 {high_level_tasks[i].task_id} 规划超时，使用简化计划")
                # 使用简化计划
                valid_steps.append(self._generate_fallback_plan(high_level_tasks[i]))
            elif isinstance(result, Exception):
                logger.error(f"任务 {high_level_tasks[i].task_id} 规划失败: {result}")
                valid_steps.append(self._generate_fallback_plan(high_level_tasks[i]))
            else:
                valid_steps.append(result)

        # 合并计划
        final_plan = plan_merger.merge_plans(high_level_tasks, valid_steps)

    except asyncio.TimeoutError:
        logger.error("总规划时间超时，回退到中心化规划")
        return await original_planner_node(state)

    return Command(...)

def _generate_fallback_plan(task: HighLevelTask) -> List[DetailedStep]:
    """生成简化的回退计划"""
    return [
        DetailedStep(
            step_id=f"{task.task_id}_fallback",
            agent_name="general_agent",  # 使用通用智能体
            description=task.description,
            tools=[],
            expected_output="完成任务"
        )
    ]
```

### 风险 5: 监控指标不足以验证收益

**问题**：无法证明"提升 71%"等性能目标

**缓解策略**：

```python
class PlanningEvaluator:
    """规划评估器"""

    def __init__(self):
        self.test_cases = self._load_test_cases()

    def _load_test_cases(self) -> List[dict]:
        """加载离线评测用例"""
        return [
            {
                "id": "test_001",
                "query": "分析100个PDF文档并生成报告",
                "expected_domains": ["document_analysis", "data_processing", "reporting"],
                "expected_steps_range": (5, 10),
                "complexity": "high"
            },
            {
                "id": "test_002",
                "query": "搜索最新的AI论文并总结",
                "expected_domains": ["web_research", "reporting"],
                "expected_steps_range": (3, 6),
                "complexity": "medium"
            },
            # ... 更多测试用例
        ]

    async def evaluate_planner(
        self,
        planner_type: Literal["centralized", "hierarchical"]
    ) -> dict:
        """评估规划器性能"""

        results = {
            "planner_type": planner_type,
            "test_cases": [],
            "summary": {}
        }

        for test_case in self.test_cases:
            start_time = time.time()

            # 执行规划
            if planner_type == "centralized":
                plan = await original_planner_node({"messages": [{"content": test_case["query"]}]})
            else:
                plan = await hierarchical_planner_node({"messages": [{"content": test_case["query"]}]})

            planning_time = time.time() - start_time

            # 评估质量
            quality_score = self._evaluate_plan_quality(plan, test_case)

            results["test_cases"].append({
                "id": test_case["id"],
                "planning_time": planning_time,
                "quality_score": quality_score,
                "num_steps": len(plan["steps"])
            })

        # 汇总统计
        results["summary"] = {
            "avg_planning_time": np.mean([r["planning_time"] for r in results["test_cases"]]),
            "avg_quality_score": np.mean([r["quality_score"] for r in results["test_cases"]]),
            "total_test_cases": len(self.test_cases)
        }

        return results

    def _evaluate_plan_quality(self, plan: dict, test_case: dict) -> float:
        """评估计划质量 (0-1)"""
        score = 0.0

        # 1. 步骤数合理性 (0.3)
        num_steps = len(plan["steps"])
        expected_range = test_case["expected_steps_range"]
        if expected_range[0] <= num_steps <= expected_range[1]:
            score += 0.3

        # 2. 领域覆盖 (0.4)
        plan_domains = set([step.get("domain") for step in plan["steps"]])
        expected_domains = set(test_case["expected_domains"])
        domain_overlap = len(plan_domains & expected_domains) / len(expected_domains)
        score += 0.4 * domain_overlap

        # 3. 依赖关系合理性 (0.3)
        has_valid_dependencies = self._check_dependencies(plan["steps"])
        if has_valid_dependencies:
            score += 0.3

        return score

# 在线监控指标
class OnlineMetrics:
    """在线监控指标"""

    @staticmethod
    def log_planning_metrics(metrics: PlanningMetrics):
        """记录规划指标到监控系统"""

        # 发送到 Prometheus/Grafana
        prometheus_client.Gauge('planning_time_seconds').set(metrics.total_planning_time)
        prometheus_client.Gauge('meta_planning_time_seconds').set(metrics.meta_planning_time)
        prometheus_client.Gauge('expert_planning_time_seconds').set(metrics.expert_planning_time)
        prometheus_client.Counter('planning_requests_total').inc()

        if metrics.cache_hit:
            prometheus_client.Counter('planning_cache_hits_total').inc()
```

**评测流程**：

```bash
# 运行离线评测
python scripts/evaluate_planner.py --planner centralized --output results/centralized.json
python scripts/evaluate_planner.py --planner hierarchical --output results/hierarchical.json

# 对比结果
python scripts/compare_results.py results/centralized.json results/hierarchical.json
```

## 十一、最小可行落地方案（MVP）

基于风险缓解，推荐的最小可行实施方案：

### 第一阶段（2周）：基础设施 + 回退机制

1. **向量数据库集成**（Chroma，本地开发）
2. **智能体聚类**（硬编码 4 个核心领域）
3. **路由置信度 + 回退逻辑**（低置信度回退到中心化）
4. **超时控制**（30秒专家规划超时）

### 第二阶段（2周）：分层规划 + 计划合并

1. **元规划器**（简化版，无检索）
2. **领域专家规划器**（4 个领域）
3. **计划合并**（拓扑排序 + 基础冲突检测）
4. **A/B 测试框架**

### 第三阶段（2周）：检索增强 + 质量闭环

1. **计划检索**（向量检索 + 时间衰减）
2. **执行反馈采集**（success_score 计算）
3. **离线评测集**（10 个核心测试用例）
4. **在线监控**（Prometheus 指标）

### 第四阶段（持续）：优化迭代

1. **扩展领域**（从 4 个到 8 个）
2. **多领域并行规划**
3. **负样本学习**
4. **自适应领域分类**

## 十二、未来扩展

1. **自适应领域分类**：根据使用情况自动调整领域划分
2. **多模态规划**：支持图像、视频等多模态任务规划
3. **强化学习优化**：基于执行反馈优化规划策略
4. **跨会话学习**：跨用户共享成功计划（隐私保护）
