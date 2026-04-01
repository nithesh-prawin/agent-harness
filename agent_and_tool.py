import os
import subprocess
import readline
from openai import OpenAI 
from dotenv import load_dotenv
import json
import logging

logging.basicConfig(level=logging.INFO)


load_dotenv()

# UTF-8, backspace fix for macOS libedit
readline.parse_and_bind('set bind-tty-special-chars off')
readline.parse_and_bind('set input-meta on')
readline.parse_and_bind('set output-meta on')
readline.parse_and_bind('set convert-meta off')
readline.parse_and_bind('set enable-meta-keybindings on')


client = OpenAI(
    api_key=os.environ["OPENROUTER_API_KEY"],  
    base_url=os.environ["OPENROUTER_BASE_URL"]
)

MODEL = os.environ["MODEL_ID"]


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": (
                "Execute a shell command in the current working directory. "
                "Use this for file operations, listing files, reading files, etc. "
                "Do NOT use for dangerous commands like deleting  files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute"
                    }
                },
                "required": ["command"]
            }
        }
    }
]

def ask_llm(prompt: str) :
    logging.info("Sending prompt to LLM: %s", prompt)
    response = client.chat.completions.create(
        model=MODEL,
        tools=TOOLS,
        messages=[
            {
                "role": "system", 
                "content": "You are a coding agent. Use bash to solve tasks, Act, don't explain."
                },
            {
                "role": "user", 
                "content": prompt
                }
        ],
        max_tokens=500
    )

    return response.choices[0].message


def run_bash(command: str) -> str:
    dangerous = ["rm", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        logging.warning("Blocked dangerous command: %s", command)
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    


def agent_run(message):

    while True:
        msg = ask_llm(message) 

        result = None
        if msg.tool_calls:
            logging.info("Tool calls detected")
            for tool_call in msg.tool_calls:
                if tool_call.function.name == "run_bash":
                    args = json.loads(tool_call.function.arguments)
                    logging.info("Executing command: %s", args["command"])
                    print(run_bash(args["command"]))
                    return 
                    

        if not result:
            logging.info("No tool calls or empty result, ending agent run")
            break
        
        
if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        logging.info("User query: %s", query)
        agent_run(query)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()