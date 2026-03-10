import asyncio
import importlib
import logging
from pathlib import Path
from typing import Dict, List, Optional

from .skill import Skill

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


class SkillsManager:
    """技能管理器，负责技能的加载、注册和执行"""
    
    def __init__(self, skills_dir: Path):
        """初始化技能管理器
        
        Args:
            skills_dir: 技能目录路径
        """
        self.skills_dir = skills_dir
        if not self.skills_dir.exists():
            logger.info(f"Skills directory {self.skills_dir} does not exist, creating...")
            self.skills_dir.mkdir(parents=True, exist_ok=True)
        
        self.available_skills: Dict[str, Skill] = {}
    
    async def initialize(self):
        """异步初始化技能管理器，加载所有技能"""
        await self._load_skills()
        logger.info(f"SkillsManager initialized. {len(self.available_skills)} skills available.")
    
    async def _load_skills(self):
        """加载技能目录中的所有技能"""
        # 加载内置技能
        await self._load_builtin_skills()
        
        # 加载自定义技能
        await self._load_custom_skills()
    
    async def _load_builtin_skills(self):
        """加载内置技能"""
        # 这里可以加载系统内置的技能
        pass
    
    async def _load_custom_skills(self):
        """加载自定义技能"""
        # 遍历技能目录，加载所有技能模块
        for skill_file in self.skills_dir.glob("**/*.py"):
            if skill_file.name == "__init__.py":
                continue
            
            # 计算模块路径
            relative_path = skill_file.relative_to(self.skills_dir.parent)
            module_path = str(relative_path).replace("\\", ".").replace(".py", "")
            
            try:
                # 导入技能模块
                module = importlib.import_module(module_path)
                
                # 查找模块中的技能类
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if isinstance(attr, type) and issubclass(attr, Skill) and attr != Skill:
                        # 实例化技能
                        skill_instance = attr()
                        self.available_skills[skill_instance.name] = skill_instance
                        logger.info(f"Loaded skill: {skill_instance.name}")
            except Exception as e:
                logger.error(f"Error loading skill from {skill_file}: {e}")
    
    async def execute_skill(self, skill_name: str, **kwargs) -> Dict:
        """执行指定技能
        
        Args:
            skill_name: 技能名称
            **kwargs: 技能输入参数
            
        Returns:
            Dict: 技能执行结果
            
        Raises:
            ValueError: 技能不存在
            ValueError: 输入参数验证失败
        """
        if skill_name not in self.available_skills:
            raise ValueError(f"Skill {skill_name} not found")
        
        skill = self.available_skills[skill_name]
        
        # 验证输入参数
        if not skill.validate_input(**kwargs):
            raise ValueError("Invalid input parameters for skill")
        
        # 执行技能
        try:
            result = await skill.execute(**kwargs)
            return result
        except Exception as e:
            logger.error(f"Error executing skill {skill_name}: {e}")
            raise
    
    def get_skill(self, skill_name: str) -> Optional[Skill]:
        """获取指定技能
        
        Args:
            skill_name: 技能名称
            
        Returns:
            Optional[Skill]: 技能实例，如果不存在则返回 None
        """
        return self.available_skills.get(skill_name)
    
    def list_skills(self) -> List[Skill]:
        """列出所有可用技能
        
        Returns:
            List[Skill]: 技能列表
        """
        return list(self.available_skills.values())
    
    def list_skills_by_category(self, category: str) -> List[Skill]:
        """按类别列出技能
        
        Args:
            category: 技能类别
            
        Returns:
            List[Skill]: 技能列表
        """
        return [skill for skill in self.available_skills.values() if skill.category == category]
