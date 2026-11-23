"""Минимальный MCP-клиент: запускает Serena так же, как это сделает Claude Code, и вызывает инструменты.

Использование:
  serena_client.py list                       — список инструментов и их параметры
  serena_client.py call serena_scenarios.json <out.json> — вызвать инструменты, записать ответы
Запускать интерпретатором с пакетом mcp, например: uvx --from serena-agent==2.1.0 python serena_client.py list
"""
import asyncio
import json
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = str(__import__("pathlib").Path(__file__).resolve().parents[3])
# Та же команда, что регистрирует install.sh (claude mcp add --scope user serena ...).
SERVER = StdioServerParameters(
    command="uvx",
    args=["-q", "-p", "3.13", "--from", "serena-agent==2.1.0", "serena", "start-mcp-server",
          "--context", "claude-code", "--project-from-cwd", "--agent-interface", "tools",
          "--enable-web-dashboard", "false"],
    cwd=REPO,
    env={**__import__("os").environ, "SERENA_USAGE_REPORTING": "false"},
)


def text_of(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def main() -> None:
    async with stdio_client(SERVER) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            if sys.argv[1] == "list":
                for t in tools:
                    print(t.name, json.dumps(list(t.input_schema.get("properties", {}).keys())))
                defs = json.dumps([t.model_dump(mode="json") for t in tools], ensure_ascii=False)
                print("TOOL_DEFS_BYTES", len(defs.encode()))
                return
            out = []
            for sc in json.load(open(sys.argv[2])):
                t0 = time.time()
                res = await s.call_tool(sc["tool"], sc["args"])
                txt = text_of(res)
                out.append({**sc, "bytes": len(txt.encode()), "secs": round(time.time() - t0, 1), "text": txt})
                print(f'{sc["id"]:28} {sc["tool"]:28} {len(txt.encode()):>7} B  {time.time() - t0:5.1f}s', flush=True)
            json.dump(out, open(sys.argv[3], "w"), ensure_ascii=False, indent=1)


asyncio.run(main())
