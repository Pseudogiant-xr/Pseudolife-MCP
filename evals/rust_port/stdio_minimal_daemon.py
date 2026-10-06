"""Use the guarded stdio daemon adapter with a minimal initial toolset."""
import os
from .stdio_daemon import main

if __name__ == "__main__":
    os.environ["PSEUDOLIFE_MCP_TOOLSET"] = "minimal"
    main()
