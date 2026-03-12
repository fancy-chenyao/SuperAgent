import json
import logging
from src.workflow.template import WORKFLOW_TEMPLATE
from typing import Union, List
from src.interface.agent import Agent
from config.global_variables import agents_dir, mermaid_enabled
from src.interface.agent import Component
import os
from pathlib import Path
from collections import deque
import re
import threading

logger = logging.getLogger(__name__)

# 工作流缓存单例类：用于管理用户工作流的缓存、持久化和执行队列
class WorkflowCache:
    _instance = None
    

    def __new__(cls, *args, **kwargs):
        """实现单例模式，确保全局只有一个WorkflowCache实例"""
        if not cls._instance:
            cls._instance = super(WorkflowCache, cls).__new__(cls)
        return cls._instance
    

    def __init__(self, workflow_dir: Path):
        """初始化工作流缓存目录和数据结构
        
        Args:
            workflow_dir: 工作流存储目录路径
        """
        if not hasattr(self, 'initialized'): 
            if not workflow_dir.exists():
                logger.info(f"path {workflow_dir} does not exist when workflow cache initializing, gona to create...")
                workflow_dir.mkdir(parents=True, exist_ok=True)
            self.workflow_dir = workflow_dir
            self.queue = {}  # 工作流执行队列：workflow_id -> deque
            self.cache = {}  # 工作流缓存数据：workflow_id -> workflow数据
            self.latest_polish_id = {}  # 用户最新的polish_id：user_id -> polish_id
            self.initialized = True
            self._lock_pool = {}  # 用户级锁池：user_id -> threading.Lock

    def _load_workflow(self, user_id: str):
        """加载指定用户的全部工作流文件到缓存
        
        Args:
            user_id: 用户ID
        """
        try:
            if user_id not in self._lock_pool:
                self._lock_pool[user_id] = threading.Lock()
            with self._lock_pool[user_id]:
                user_workflow_dir = self.workflow_dir / user_id
                if not user_workflow_dir.exists():
                    # only create user workflow dir
                    logger.info(f"path {user_workflow_dir} does not exist when user {user_id} workflow cache initializing, gona to create...")
                    user_workflow_dir.mkdir(parents=True, exist_ok=True)
                    return

                # user workflow dir exists, then load workflow
                user_workflow_files = user_workflow_dir.glob("*.json")
                for workflow_file in user_workflow_files:
                    with open(workflow_file, "r", encoding='utf-8') as f:
                        workflow = json.load(f)
                        self.cache[workflow["workflow_id"]] = workflow
        except Exception as e:
            logger.error(f"Error loading workflow: {e}")
            raise e

    def init_cache(self, user_id: str, lap: int, mode: str, workflow_id: str, version: int, user_input_messages: list, deep_thinking_mode: bool, search_before_planning: bool, coor_agents: list[str], load_user_workflow: bool = True):
        """初始化工作流缓存
        
        Args:
            user_id: 用户ID
            lap: 迭代轮次
            mode: 工作模式（launch/production等）
            workflow_id: 工作流ID
            version: 版本号
            user_input_messages: 用户输入消息
            deep_thinking_mode: 深度思考模式
            search_before_planning: 规划前搜索
            coor_agents: 协调器代理列表
            load_user_workflow: 是否加载用户工作流
        """
        try:
            self._load_workflow(user_id)
            with self._lock_pool[user_id]:
                if mode == "launch":
                    # 新建工作流：使用模板初始化
                    self.cache[workflow_id] = WORKFLOW_TEMPLATE.copy()
                    self.cache[workflow_id]["mode"] = mode
                    self.cache[workflow_id]["lap"] = lap
                    self.cache[workflow_id]["workflow_id"] = workflow_id
                    self.cache[workflow_id]["version"] = version
                    self.cache[workflow_id]["user_input_messages"] = user_input_messages
                    self.cache[workflow_id]["deep_thinking_mode"] = deep_thinking_mode
                    self.cache[workflow_id]["search_before_planning"] = search_before_planning
                    self.cache[workflow_id]["coor_agents"] = coor_agents
                else:
                    # 恢复工作流：从文件加载
                    try:
                        if workflow_id not in self.cache:
                            #todo: user workflow.json file not exist, how to handle?
                            user_id, polish_id = workflow_id.split(":")
                            user_workflow_dir = self.workflow_dir / user_id
                            user_workflow_file = user_workflow_dir / polish_id
                            with open(str(user_workflow_file) + '.json', 'r', encoding='utf-8') as f:
                                workflow = json.load(f)
                                if workflow:
                                    self.cache[workflow["workflow_id"]] = workflow
                                else:
                                    logger.error(f"Error loading workflow {user_workflow_file} for user {user_id}: {e}")
                                    raise Exception(f"Error loading workflow {user_workflow_file} for user {user_id}")

                        # 初始化执行队列
                        self.queue[workflow_id] = deque()
                        for agent in self.cache[workflow_id]["graph"]:
                            if agent["config"]["node_type"] == "execution_agent":
                                self.queue[workflow_id].append(agent)
                        # 添加起始节点
                        begin_node = {
                            "component_type": "agent",
                            "label": "begin_node",
                            "name": "begin_node",
                            "config": {
                                "node_name": "begin_node",
                                "node_type": "execution_agent",
                                "next_to": [self.queue[workflow_id][0]["config"]["node_name"]],
                                "condition": "supervised"
                            }
                        }
                        self.queue[workflow_id].appendleft(begin_node)
                    except Exception as e:
                        logger.error(f"Error initializing workflow cache: {e}")
                        raise e
        except Exception as e:
            logger.error(f"Error initializing workflow cache: {e}")
            raise e
    
    def list_workflows(self, user_id: str, match: str = None):
        """列出指定用户的所有工作流，可选正则匹配
        
        Args:
            user_id: 用户ID
            match: 正则匹配模式
            
        Returns:
            工作流列表
        """
        self._load_workflow(user_id)
        user_workflow_dir = self.workflow_dir / user_id
        user_workflow_files = user_workflow_dir.glob("*.json")
        workflows = []
        for workflow_file in user_workflow_files:
            filename = workflow_file.stem
            if match:
                if re.match(match, filename):
                    workflows.append(self.cache[user_id + ":" + filename])
            else:
                workflows.append(self.cache[user_id + ":" + filename])
        return workflows
            
    def get_latest_polish_id(self, user_id: str):
        """获取用户最新的polish_id（基于文件修改时间）
        
        Args:
            user_id: 用户ID
            
        Returns:
            最新的polish_id，如果不存在则返回None
        """
        if user_id not in self.latest_polish_id or not self.latest_polish_id[user_id]:
            user_workflow_dir = self.workflow_dir / user_id
            latest_file = None
            latest_mtime = 0
            polish_id_to_set = None

            if user_workflow_dir.exists():
                workflow_files = list(user_workflow_dir.glob("*.json"))
                if workflow_files:
                    for workflow_file in workflow_files:
                        try:
                            mtime = os.path.getmtime(workflow_file)
                            if mtime > latest_mtime:
                                latest_mtime = mtime
                                latest_file = workflow_file
                        except OSError as e:
                            logger.warning(f"Could not get mtime for {workflow_file}: {e}")
                    
                    if latest_file:
                        polish_id_to_set = latest_file.stem # filename without extension
                        try:
                            with open(latest_file, "r", encoding='utf-8') as f:
                                workflow_data = json.load(f)
                                workflow_id = user_id + ":" + polish_id_to_set
                                self.cache[workflow_id] = workflow_data
                            logger.info(f"Loaded latest polish workflow {polish_id_to_set} for user {user_id} from {latest_file}")
                        except Exception as e:
                            logger.error(f"Error loading latest polish workflow {latest_file} for user {user_id}: {e}")
                            polish_id_to_set = None # Failed to load
            
                        with self._lock_pool[user_id]:
                            self.latest_polish_id[user_id] = polish_id_to_set
            if polish_id_to_set is None:
                logger.info(f"No suitable polish workflow found for user {user_id} in {user_workflow_dir}")

        return self.latest_polish_id.get(user_id)
        
    def restore_planning_steps(self, workflow_id: str, planning_steps, user_id: str):
        """恢复规划步骤到缓存
        
        Args:
            workflow_id: 工作流ID
            planning_steps: 规划步骤列表
            user_id: 用户ID
        """
        try:
            if user_id not in self._lock_pool:
                self._lock_pool[user_id] = threading.Lock()
            with self._lock_pool[user_id]:
                self.cache[workflow_id]["planning_steps"] = planning_steps
        except Exception as e:
            logger.error(f"Error restoring planning steps: {e}")
            with self._lock_pool[user_id]:
                self.cache[workflow_id]["planning_steps"] = []

    def get_planning_steps(self, workflow_id: str):
        """获取工作流的规划步骤
        
        Args:
            workflow_id: 工作流ID
            
        Returns:
            规划步骤列表
        """
        return self.cache[workflow_id].get("planning_steps", [])
            
    def update_stack(self, workflow_id: str, user_id: str):
        """更新执行队列，移除已完成的节点
        
        Args:
            workflow_id: 工作流ID
            user_id: 用户ID
        """
        if user_id not in self._lock_pool:
            self._lock_pool[user_id] = threading.Lock()
        with self._lock_pool[user_id]:
            self.queue[workflow_id].popleft()
    
    def get_next_node(self, workflow_id: str):
        """获取下一个要执行的节点
        
        Args:
            workflow_id: 工作流ID
            
        Returns:
            下一个节点名称，如果结束则返回"FINISH"
        """
        try:
            if not self.queue[workflow_id][0]["config"]["next_to"] or self.queue[workflow_id][0]["config"]["next_to"][0] == "__end__":
                return "FINISH"
            else:
                return self.queue[workflow_id][0]["config"]["next_to"][0]
             
        except Exception as e:
            logger.error(f"Error getting next node: {e}")
            return "FINISH"
    
    def get_lap(self, workflow_id: str):
        """获取工作流的迭代轮次
        
        Args:
            workflow_id: 工作流ID
            
        Returns:
            迭代轮次
        """
        try:
            return self.cache[workflow_id]["lap"]
        except Exception as e:
            logger.error(f"Error getting lap: {e}")

    def restore_system_node(self, workflow_id: str, node: Union[Component, str], user_id: str):
        """恢复系统节点到工作流缓存
        
        Args:
            workflow_id: 工作流ID
            node: 节点对象或节点名称字符串
            user_id: 用户ID
        """
        try:
            logger.info(f"restore_system_node node: {node}")
            if isinstance(node, Component):
                if user_id not in self._lock_pool:
                    self._lock_pool[user_id] = threading.Lock()
                with self._lock_pool[user_id]:
                    if node.name not in self.cache[workflow_id]["nodes"]:
                        self.cache[workflow_id]["nodes"][node.name] = node.model_dump()
                    for existing_node in self.cache[workflow_id]["graph"]:
                        if existing_node["name"] == node.name:
                            return  # 节点已存在，不添加
                    self.cache[workflow_id]["graph"].append({
                        "component_type": node.component_type,
                        "label": node.label,
                        "name": node.name,
                        "config": {
                            "node_name": node.name,
                            "node_type": node.config["type"],
                            "next_to": [],
                            "condition": {}
                        }
                    })
            elif isinstance(node, str):
                _next_to = node
                if self.cache[workflow_id]["graph"][-1]["config"]["node_type"] == "system_agent":
                    if not self.cache[workflow_id]["graph"][-1]["config"]["next_to"]:
                        self.cache[workflow_id]["graph"][-1]["config"]["next_to"].append(_next_to)

        except Exception as e:
            logger.error(f"Error restore_system_node: {e}")

    def restore_node(self, workflow_id: str, node: Union[Agent, str], workflow_initialized: bool, user_id: str):
        """恢复代理节点到工作流缓存
        
        Args:
            workflow_id: 工作流ID
            node: 代理对象或节点名称字符串
            workflow_initialized: 工作流是否已初始化
            user_id: 用户ID
        """
        # todo: restore_node and restore_system_node can be merged
        try:
            logger.info(f"restore_node node: {node}")
            if isinstance(node, Agent):
                _agent = node
                if user_id not in self._lock_pool:
                    self._lock_pool[user_id] = threading.Lock()
                with self._lock_pool[user_id]:
                    if _agent.agent_name not in self.cache[workflow_id]["nodes"]:
                        tools = []
                        for tool in _agent.selected_tools:
                            tools.append({
                                "component_type": "function",
                                "label": tool.name,
                                "name": tool.name,
                                "config": {
                                    "name": tool.name,
                                    "description": tool.description,
                                }
                            })
                        self.cache[workflow_id]["nodes"][_agent.agent_name] = {
                            "component_type": "agent",
                            "label": _agent.agent_name,
                            "name": _agent.agent_name,
                            "config": {
                                "name": _agent.agent_name,
                                "tools": tools,
                                "description": _agent.description,
                                "prompt": _agent.prompt
                            }
                        }
                    self.cache[workflow_id]["graph"].append({
                        "component_type": "agent",
                        "label": _agent.agent_name,
                        "name": _agent.agent_name,
                        "config": {
                            "node_name": _agent.agent_name,
                            "node_type": "execution_agent",
                            "next_to": [],
                            "condition": "supervised"
                        }

                    })

            elif isinstance(node, str) and workflow_initialized:
                _next_to = node
                if _next_to == "__end__" :
                    return
                if self.cache[workflow_id]["graph"][-1]["config"]["node_type"] == "execution_agent":
                    with self._lock_pool[user_id]:
                        if not self.cache[workflow_id]["graph"][-1]["config"]["next_to"]:
                            self.cache[workflow_id]["graph"][-1]["config"]["next_to"].append(_next_to)
        except Exception as e:
            logger.error(f"Error restore_node: {e}")

    def __reduce__(self):
        return super().__reduce__()

    def save_planning_steps(self, workflow_id, planning_steps):
        """保存规划步骤到工作流文件
        
        Args:
            workflow_id: 工作流ID
            planning_steps: 规划步骤列表
        """
        try:
            workflow = self.cache[workflow_id]
            user_id, polish_id = workflow["workflow_id"].split(":")
            workflow_path = self.workflow_dir / user_id / f"{polish_id}.json"

            if user_id not in self._lock_pool:
                self._lock_pool[user_id] = threading.Lock()
            with self._lock_pool[user_id]:
                self.cache[workflow_id]["planning_steps"] = json.dumps(planning_steps, ensure_ascii=False)
                workflow = self.cache[workflow_id]

                with open(workflow_path, "w", encoding='utf-8') as f:
                    f.write(json.dumps(workflow, indent=2, ensure_ascii=False))
            # 同步保存流程图 + 自检
            if mermaid_enabled:
                mermaid_path = self._save_mermaid(workflow_id)
                if not mermaid_path or not os.path.exists(mermaid_path):
                    logger.warning(f"Mermaid visualization not saved for {workflow_id}")
        except Exception as e:
            logger.error(f"Error dumping workflow: {e}")
    def save_workflow(self, workflow):
        """保存工作流到文件
        
        Args:
            workflow: 工作流数据字典
        """
        try:

            user_id, polish_id = workflow["workflow_id"].split(":")
            workflow_path = self.workflow_dir / user_id / f"{polish_id}.json"

            if user_id not in self._lock_pool:
                self._lock_pool[user_id] = threading.Lock()
            with self._lock_pool[user_id]:
                with open(workflow_path, "w", encoding='utf-8') as f:
                    f.write(json.dumps(workflow, indent=2, ensure_ascii=False))
            # 同步保存流程图 + 自检
            if mermaid_enabled:
                mermaid_path = self._save_mermaid(workflow["workflow_id"])
                if not mermaid_path or not os.path.exists(mermaid_path):
                    logger.warning(f"Mermaid visualization not saved for {workflow['workflow_id']}")
        except Exception as e:
            logger.error(f"Error dumping workflow: {e}")
        logger.info(f"workflow {workflow['workflow_id']} saved.")
    
    def _save_mermaid(self, workflow_id: str):
        """保存工作流的 Mermaid 流程图（增强版）

        Args:
            workflow_id: 工作流ID

        Returns:
            Mermaid 文件路径，失败返回 None
        """
        try:
            workflow = self.cache[workflow_id]
            graph = workflow.get("graph", [])
            nodes = workflow.get("nodes", {})

            # 开始构建 Mermaid 代码
            mermaid_code = "graph TD\n"

            def _safe_id(raw_name: str, used_ids: set[str]) -> str:
                base = re.sub(r"[^A-Za-z0-9_]", "_", str(raw_name))
                if not re.match(r"^[A-Za-z_]", base):
                    base = f"n_{base}"
                safe = base or "n"
                if safe not in used_ids:
                    used_ids.add(safe)
                    return safe
                i = 2
                while f"{safe}_{i}" in used_ids:
                    i += 1
                safe = f"{safe}_{i}"
                used_ids.add(safe)
                return safe

            def _sanitize_label(text: str) -> str:
                if text is None:
                    return ""
                if not isinstance(text, str):
                    text = str(text)
                replace_map = {
                    "，": ",",
                    "。": ".",
                    "、": ",",
                    "；": ";",
                    "：": ":",
                    "？": "?",
                    "！": "!",
                    "（": "(",
                    "）": ")",
                    "【": "[",
                    "】": "]",
                    "《": "<",
                    "》": ">",
                    "“": "\"",
                    "”": "\"",
                    "‘": "'",
                    "’": "'",
                    "—": "-",
                    "…": "...",
                }
                for k, v in replace_map.items():
                    text = text.replace(k, v)
                text = re.sub(r"[\u0000-\u001f\u007f]", " ", text)
                return text

            used_ids: set[str] = set()
            id_map: dict[str, str] = {}
            for node in graph:
                node_name = node.get("name", "unknown")
                if node_name not in id_map:
                    id_map[node_name] = _safe_id(node_name, used_ids)

            # 添加节点定义（包含描述和工具信息）
            for node in graph:
                node_name = node.get("name", "unknown")
                node_id = id_map.get(node_name, _safe_id(node_name, used_ids))
                node_type = node.get("config", {}).get("node_type", "unknown")
                condition = node.get("config", {}).get("condition", "")

                # 获取节点详细信息
                node_detail = nodes.get(node_name, {})
                if isinstance(node_detail, str):
                    try:
                        node_detail = json.loads(node_detail)
                    except Exception:
                        node_detail = {}
                description = node_detail.get("description", node_detail.get("config", {}).get("description", ""))
                if description is None:
                    description = ""
                if not isinstance(description, str):
                    description = str(description)
                tools = node_detail.get("config", {}).get("tools", [])
                if not isinstance(tools, list):
                    tools = []

                # 构建节点标签
                label_parts = [_sanitize_label(node_name)]

                # 添加节点类型
                if node_type == "execution_agent":
                    label_parts.append("执行代理")
                elif node_type == "system_agent":
                    label_parts.append("系统代理")

                # 添加简短描述（限制长度）
                if description:
                    short_desc = description[:50] + "..." if len(description) > 50 else description
                    label_parts.append(_sanitize_label(short_desc))

                # 添加工具信息
                if tools:
                    tool_names = [
                        t.get("name", t.get("label", "")) if isinstance(t, dict) else str(t)
                        for t in tools
                    ]
                    if tool_names:
                        label_parts.append(f"工具: {', '.join([_sanitize_label(n) for n in tool_names[:3]])}")

                # 添加条件信息
                if condition:
                    label_parts.append(f"条件: {_sanitize_label(condition)}")

                # 生成节点定义
                label = "<br>".join(label_parts)

                # 根据节点类型使用不同的形状和样式
                if node_type == "execution_agent":
                    mermaid_code += f'    {node_id}[({label})]\n'
                    mermaid_code += f'    style {node_id} fill:#e1f5ff,stroke:#01579b,stroke-width:2px\n'
                elif node_type == "system_agent":
                    mermaid_code += f'    {node_id}[({label})]\n'
                    mermaid_code += f'    style {node_id} fill:#fff3e0,stroke:#e65100,stroke-width:2px\n'
                else:
                    mermaid_code += f'    {node_id}[({label})]\n'

            mermaid_code += "\n"

            # 添加边（连接关系）
            for node in graph:
                node_name = node.get("name", "unknown")
                node_id = id_map.get(node_name, _safe_id(node_name, used_ids))
                next_to = node.get("config", {}).get("next_to", [])
                for target in next_to:
                    if target != "__end__":
                        target_id = id_map.get(target)
                        if not target_id:
                            target_id = _safe_id(target, used_ids)
                            id_map[target] = target_id
                            mermaid_code += f'    {target_id}[({target})]\n'
                        mermaid_code += f'    {node_id} --> {target_id}\n'
                    else:
                        mermaid_code += f'    {node_id} --> END[(结束)]\n'
                        mermaid_code += f'    style END fill:#ffebee,stroke:#c62828,stroke-width:2px\n'

            # 保存到文件
            user_id, polish_id = workflow_id.split(":")
            output_path = self.workflow_dir / user_id / f"{polish_id}_visualization.md"

            with open(output_path, "w", encoding="utf-8") as f:
                # 写入标题和元数据
                f.write("# 工作流可视化\n\n")
                f.write("## 工作流信息\n\n")
                f.write(f"- **工作流ID**: {workflow_id}\n")
                f.write(f"- **版本**: {workflow.get('version', 'N/A')}\n")
                f.write(f"- **迭代轮次**: {workflow.get('lap', 'N/A')}\n")
                f.write(f"- **模式**: {workflow.get('mode', 'N/A')}\n")
                f.write(f"- **深度思考模式**: {workflow.get('deep_thinking_mode', 'N/A')}\n")
                f.write(f"- **规划前搜索**: {workflow.get('search_before_planning', 'N/A')}\n")

                # 协调器代理
                coor_agents = workflow.get('coor_agents', [])
                if coor_agents:
                    f.write(f"- **协调器代理**: {', '.join(coor_agents)}\n")

                f.write("\n## 流程图\n\n")
                f.write("```mermaid\n")
                f.write(mermaid_code)
                f.write("```\n\n")

                # 添加节点详细信息表格
                f.write("## 节点详细信息\n\n")
                f.write("| 节点名称 | 类型 | 描述 | 工具 |\n")
                f.write("|---------|------|------|------|\n")

                for node in graph:
                    node_name = node.get("name", "unknown")
                    node_type = node.get("config", {}).get("node_type", "unknown")
                    node_detail = nodes.get(node_name, {})
                    if isinstance(node_detail, str):
                        try:
                            node_detail = json.loads(node_detail)
                        except Exception:
                            node_detail = {}
                    description = node_detail.get("description", node_detail.get("config", {}).get("description", "无"))
                    if description is None:
                        description = ""
                    if not isinstance(description, str):
                        description = str(description)
                    tools = node_detail.get("config", {}).get("tools", [])
                    if not isinstance(tools, list):
                        tools = []
                    tool_names = ", ".join([
                        t.get("name", t.get("label", "")) if isinstance(t, dict) else str(t)
                        for t in tools
                    ]) if tools else "无"

                    type_cn = "执行代理" if node_type == "execution_agent" else "系统代理" if node_type == "system_agent" else node_type

                    f.write(f"| {node_name} | {type_cn} | {description[:100]}... | {tool_names} |\n")

                # 添加规划步骤
                planning_steps = workflow.get("planning_steps", [])
                if planning_steps:
                    f.write("\n## 规划步骤\n\n")
                    if isinstance(planning_steps, str):
                        try:
                            planning_steps = json.loads(planning_steps)
                        except:
                            pass

                    if isinstance(planning_steps, list):
                        for i, step in enumerate(planning_steps, 1):
                            if isinstance(step, dict):
                                agent_name = step.get("agent_name", "未知")
                                description = step.get("description", "无描述")
                                f.write(f"{i}. **{agent_name}**: {description}\n")
                            else:
                                f.write(f"{i}. {step}\n")

            logger.info(f"Saved Mermaid visualization to {output_path}")
            return str(output_path)

        except Exception as e:
            logger.error(f"Error saving Mermaid visualization: {e}")
            return None
    
    def dump(self, workflow_id: str, mode: str):
        """将工作流数据持久化到文件
        
        Args:
            workflow_id: 工作流ID
            mode: 工作模式（launch/production）
        """
        try:
            workflow = self.cache[workflow_id]
            user_id, polish_id = workflow["workflow_id"].split(":")
            if user_id not in self._lock_pool:
                self._lock_pool[user_id] = threading.Lock()
            with self._lock_pool[user_id]:
                if mode == "launch":
                    # 新建模式：保存工作流并更新最新polish_id
                    workflow = self.cache[workflow_id]
                    user_id, polish_id = workflow["workflow_id"].split(":")
                    workflow_path = self.workflow_dir / user_id / f"{polish_id}.json"
                    with open(workflow_path, "w", encoding='utf-8') as f:
                        f.write(json.dumps(workflow, indent=2, ensure_ascii=False))
                    self.latest_polish_id[user_id] = polish_id
                    
                    # 保存 Mermaid 可视化
                    if mermaid_enabled:
                        self._save_mermaid(workflow_id)
                elif mode == "production":
                    # 生产模式：清空执行队列
                    self.queue[workflow_id] = []
        except Exception as e:
            logger.error(f"Error dumping workflow: {e}")
            
    def get_editable_agents(self, workflow_id: str) -> List[Agent]:
        """获取工作流中可编辑的代理列表
        
        Args:
            workflow_id: 工作流ID
            
        Returns:
            代理对象列表
        """
        try:
            agents = []
            for node in self.cache[workflow_id]["graph"]:
                if node["config"]["node_type"] == "execution_agent":
                    agent_path = agents_dir / f"{node["config"]["node_name"]}.json"
                    with open(agent_path, "r", encoding="utf-8") as f:
                        json_str = f.read()
                        _agent = Agent.model_validate_json(json_str)
                    agents.append(_agent)
            return agents
        except Exception as e:
            logger.error(f"Error getting agents: {e}")
            return []
         
        
        
from config.global_variables import workflows_dir

# 创建全局WorkflowCache单例实例
workflow_cache = WorkflowCache(workflow_dir=workflows_dir)
