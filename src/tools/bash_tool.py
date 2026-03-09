import logging
import subprocess
from typing import ClassVar, Type
from pydantic import BaseModel, Field
from langchain.tools import BaseTool
from .decorators import create_logged_tool

# Initialize logger
logger = logging.getLogger(__name__)


class BashToolInput(BaseModel):
    """Input for Bash Tool."""
    cmd: str = Field(..., description="The bash command to be executed.")


class BashTool(BaseTool):
    name: ClassVar[str] = "bash_tool"
    args_schema: Type[BaseModel] = BashToolInput
    description: ClassVar[str] = "Use this to execute bash command and do necessary operations."

    def _run(self, cmd: str) -> str:
        """Execute bash command and return result."""
        logger.info(f"Executing Bash Command: {cmd}")
        
        # Handle platform-specific commands
        import platform
        if platform.system() == "Windows":
            # Map common Unix commands to Windows equivalents
            cmd_map = {
                "pwd": "cd",
                "ls": "dir",
                "ls -l": "dir",
                "ls -all": "dir",
                "clear": "cls",
                "touch": "type nul >",
                "rm": "del",
                "cp": "copy",
                "mv": "move",
                "cat": "type"
            }
            
            # Simple command replacement
            cmd_parts = cmd.split()
            if cmd_parts and cmd_parts[0] in cmd_map:
                cmd_parts[0] = cmd_map[cmd_parts[0]]
                cmd = " ".join(cmd_parts)
                
            # Handle here-document syntax (<<) which is not supported in Windows CMD/PowerShell
            if "<<" in cmd:
                return "Error: Here-document syntax (<<) is not supported on Windows. Please use echo or file redirection instead."

        try:
            # Execute the command and capture output
            result = subprocess.run(
                cmd, shell=True, check=True, text=True, capture_output=True
            )
            # Return stdout as the result
            return result.stdout
        except subprocess.CalledProcessError as e:
            # If command fails, return error information
            error_message = f"Command failed with exit code {e.returncode}.\nStdout: {e.stdout}\nStderr: {e.stderr}"
            logger.error(error_message)
            return error_message
        except Exception as e:
            # Catch any other exceptions
            error_message = f"Error executing command: {str(e)}"
            logger.error(error_message)
            return error_message

    async def _arun(self, cmd: str) -> str:
        """Async version of bash tool."""
        return self._run(cmd)


# Create logged version of the tool
BashTool = create_logged_tool(BashTool)
bash_tool = BashTool()


if __name__ == "__main__":
    print(bash_tool.invoke("ls -all"))
