import argparse
import os
from pathlib import Path
import tomllib

from mcp.server.mcpserver import MCPServer


parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
arguments = parser.parse_args()
configuration = tomllib.loads(Path(arguments.config).read_text(encoding="utf-8"))

mcp = MCPServer("configured")


@mcp.tool()
def configured_source() -> str:
    source = configuration["source"]
    return f'{source["name"]}:{source["query"]}:{os.environ["DB_PASSWORD"]}'


if __name__ == "__main__":
    mcp.run(transport="stdio")
