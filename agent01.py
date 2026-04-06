import logging
import os
import readline
import subprocess
from pathlib import Path

import ollama
from tabulate import tabulate

# UTF-8 backspace fix for macOS libedit
# Basically making the terminal work with UTF-8 and backspace properly as a proper CLI environment
readline.parse_and_bind("set bind-tty-special-chars off")
readline.parse_and_bind("set input-meta on")
readline.parse_and_bind("set output-meta on")
readline.parse_and_bind("set convert-meta off")
readline.parse_and_bind("set enable-meta-keybindings on")

# User defined variables
SYSTEM_PROMPT = """
You're a personal assistant capable of completing tasks and interacting with the filesystem.
You can plan multiple tasks ahead and execute them sequentially using TodoManager.
Use tools to interact with the environment and complete tasks.
Talk less and work more.
"""
HISTORY = []

# Configure logging for production observability
logger = logging.getLogger(__name__)

# Constants
WORKDIR = Path(os.getcwd()).resolve()
MAX_OUTPUT_LEN = 50000
DEFAULT_TIMEOUT = 120

TOOLS = [
    # --- TodoManager Tools ---
    {
        "type": "function",
        "function": {
            "name": "add_todo_list",
            "description": "Initialize or replace the todo list with a new set of tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_list": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "task_id": {
                                    "type": "integer",
                                    "description": "Unique ID",
                                },
                                "task": {
                                    "type": "string",
                                    "description": "Task description",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["todo", "in_progress", "completed"],
                                },
                            },
                            "required": ["task_id", "status"],
                        },
                    }
                },
                "required": ["task_list"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_todos",
            "description": "Update the status of an existing task by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["in_progress", "completed"]},
                },
                "required": ["task_id", "status"],
            },
        },
    },
    # --- System & File Tools ---
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Execute a bash command in the workspace. Restricted: no sudo/shutdown/rm.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command_string": {
                        "type": "string",
                        "description": "The full bash command to run.",
                    }
                },
                "required": ["command_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_read",
            "description": "Read the contents of a file within the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path_str": {
                        "type": "string",
                        "description": "Relative path to the file.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Optional line limit for reading.",
                    },
                },
                "required": ["path_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_write",
            "description": "Write content to a file (creates or overwrites).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path_str": {"type": "string", "description": "Relative path."},
                    "content": {
                        "type": "string",
                        "description": "The full text content to write.",
                    },
                },
                "required": ["path_str", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_edit",
            "description": "Replace a specific block of text in a file with new text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path_str": {"type": "string"},
                    "old_text": {
                        "type": "string",
                        "description": "The exact text to find.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The text to replace it with.",
                    },
                },
                "required": ["path_str", "old_text", "new_text"],
            },
        },
    },
]


# User defined functions
class TodoManager:
    def __init__(self, task_limit=20):
        self.todo_list = []
        self.task_count = 0
        self.task_limit = task_limit
        self.in_progress = 0
        self.completed = 0

    def validate_todo_list(self):

        if not self.todo_list or self.task_count == 0:
            return "No tasks found"

        if self.task_count > self.task_limit:
            return "Only {} tasks allowed".format(self.task_limit)

        for task in self.todo_list:
            if (task.get("task_id") is None or task.get("status") is None) and task.get(
                "status"
            ) not in ["in_progress", "completed"]:
                return "Invalid task format"

        return self.render_todos()

    def add_todo_list(self, task_list):
        self.todo_list = task_list
        self.task_count = len(task_list)
        return self.validate_todo_list()

    def update_todos(self, task_id, status):

        if self.completed == self.task_count:
            return "All todos completed successfully"

        for task in self.todo_list:
            if task["task_id"] == task_id:
                if status == "in_progress":
                    self.in_progress += 1
                elif status == "completed":
                    self.completed += 1
                    self.in_progress -= 1
                task["status"] = status
                return self.render_todos()

    def render_todos(self):
        return tabulate(self.todo_list, headers="keys", tablefmt="grid")


TODO_MANAGER = TodoManager()


def safe_path(p):
    """Resolves and validates that a path is strictly within the WORKDIR."""
    try:
        # .resolve() handles symlinks and '..' to find the real physical path
        path = (WORKDIR / p).resolve()
        if not path.is_relative_to(WORKDIR):
            logger.error(f"Security Alert: Attempted access outside workspace: {p}")
            raise PermissionError(f"Path escapes workspace: {p}")
        return path
    except Exception as e:
        logger.error(f"Path validation failed for '{p}': {e}")
        raise


def run_bash(command_string, timeout=DEFAULT_TIMEOUT):
    """
    Splits string into tokens and runs without shell=True for security.
    """
    # Prevent common dangerous patterns before execution
    dangerous = ["sudo", "shutdown", "reboot", "poweroff"]
    args = shlex.split(command_string)

    if not args:
        return "Error: Empty command"

    if args[0] in dangerous:
        return f"Error: Command '{args[0]}' is restricted for security."

    try:
        # Executing as a list (args) prevents shell injection
        result = subprocess.run(
            args, cwd=WORKDIR, capture_output=True, text=True, timeout=timeout
        )

        output = (result.stdout + result.stderr).strip()
        return output[:MAX_OUTPUT_LEN] if output else "(no output)"

    except subprocess.TimeoutExpired:
        logger.warning(f"Process timed out: {command_string}")
        return f"Error: Timeout after {timeout}s"
    except FileNotFoundError:
        return f"Error: Command '{args[0]}' not found."
    except Exception as e:
        logger.exception("Unexpected bash execution error")
        return f"Error: {str(e)}"


def run_read(path_str, limit=None):
    """Reads file content safely with line-based truncation."""
    try:
        target = safe_path(path_str)
        if not target.is_file():
            return f"Error: '{path_str}' is not a file."

        # Specify encoding to prevent issues with system-specific defaults
        text = target.read_text(encoding="utf-8")
        lines = text.splitlines()

        if limit and 0 < limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} lines truncated)"]

        output = "\n".join(lines)
        return output[:MAX_OUTPUT_LEN]
    except Exception as e:
        logger.error(f"Read error: {e}")
        return f"Error: Unable to read {path_str}"


def run_write(path_str, content):
    """Writes file content atomically using a temporary file."""
    try:
        target = safe_path(path_str)
        target.parent.mkdir(parents=True, exist_ok=True)

        # Atomic Write: Write to .tmp then rename.
        # This prevents corrupted files if the disk is full or process crashes.
        temp_file = target.with_suffix(f"{target.suffix}.tmp")
        temp_file.write_text(content, encoding="utf-8")
        temp_file.replace(target)

        return f"Successfully wrote {len(content)} bytes to {path_str}"
    except Exception as e:
        logger.error(f"Write error: {e}")
        return f"Error: Failed to write to {path_str}"


def run_edit(path_str, old_text, new_text):
    """Precise replacement that leverages the atomic write function."""
    try:
        target = safe_path(path_str)
        if not target.exists():
            return f"Error: File {path_str} does not exist."

        content = target.read_text(encoding="utf-8")
        if old_text not in content:
            return f"Error: Search text not found in {path_str}"

        # replace(..., 1) ensures we only change the first match for safety
        updated_content = content.replace(old_text, new_text, 1)

        # Reuse run_write for the atomic replacement logic
        result = run_write(path_str, updated_content)
        return result.replace("wrote", "edited")

    except Exception as e:
        logger.error(f"Edit error: {e}")
        return f"Error: Failed to edit {path_str}"


tool_handler = {
    "add_todo_list": lambda **kwargs: TODO_MANAGER.add_todo_list(**kwargs),
    "update_todos": lambda **kwargs: TODO_MANAGER.update_todos(**kwargs),
    "run_bash": lambda **kwargs: run_bash(**kwargs),
    "run_read": lambda **kwargs: run_read(**kwargs),
    "run_write": lambda **kwargs: run_write(**kwargs),
    "run_edit": lambda **kwargs: run_edit(**kwargs),
}


def agent_loop(query):
    HISTORY.append({"role": "user", "content": query})

    while True:
        print("Query sent to the LLM.")
        response = ollama.chat(
            model="gemma4:e4b",
            messages=HISTORY,
            tools=TOOLS,
            stream=False,
        )

        message = response.get("message", {})
        done_reason = response.get("done_reason")

        HISTORY.append(message)
        print("Received response from LLM.")

        if response.get("tool_calls"):
            print(
                f"LLM requested tool calls. Tool called : {message['tool_calls']['name']}"
            )

            for call in message["tool_calls"]:
                tool_name = call["function"]["name"]
                tool_args = call["function"]["arguments"]

                observation = ""
                if tool_name in tool_handler:
                    print(f"[*] Executing Tool: {tool_name}({tool_args})")
                    try:
                        observation = tool_handler[tool_name](**tool_args)
                    except Exception as e:
                        observation = f"Error executing tool: {str(e)}"
                else:
                    observation = f"Error: Tool {tool_name} not found."

                print(f"[*] Tool {tool_name} returned: {observation}")
                HISTORY.append(
                    {"role": "tool", "content": str(observation), "name": tool_name}
                )

        if done_reason == "stop":
            print(f"\n[Agent]: {response}")
            break

    return message.get("content")


if __name__ == "__main__":
    while True:
        try:
            query = input(">>>")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        agent_loop(query)
        print()
