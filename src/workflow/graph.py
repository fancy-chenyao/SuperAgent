from typing import Callable, Dict, List, Optional

from src.interface.agent import State

try:
    from langgraph.types import Command
except Exception:  # pragma: no cover - optional dependency in lightweight test env
    class Command:  # type: ignore
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, update=None, goto=None):
            self.update = update or {}
            self.goto = goto



NodeFunc = Callable[[State], Command]

class AgentWorkflow:
    def __init__(self):
        self.nodes: Dict[str, NodeFunc] = {}
        self.edges: Dict[str, List[str]] = {}
        self.start_node: Optional[str] = None
        
    def add_node(self, name: str, func: NodeFunc) -> None:
        self.nodes[name] = func
        
    def add_edge(self, source: str, target: str) -> None:
        if source not in self.edges:
            self.edges[source] = []
        self.edges[source].append(target)
        
    def set_start(self, node: str) -> None:
        self.start_node = node
        
    def compile(self):
        return CompiledWorkflow(self.nodes, self.edges, self.start_node)

class CompiledWorkflow:
    def __init__(self, nodes: Dict[str, NodeFunc], edges: Dict[str, List[str]], start_node: str):
        self.nodes = nodes
        self.edges = edges
        self.start_node = start_node
        
    def invoke(self, state: State) -> State:
        current_node = self.start_node
        while current_node != "__end__":
            if current_node not in self.nodes:
                raise ValueError(f"Node {current_node} not found in workflow")

            node_func = self.nodes[current_node]
            command = node_func(state)

            if hasattr(command, 'update') and command.update:
                for key, value in command.update.items():
                    state[key] = value

            current_node = command.goto

        return state
