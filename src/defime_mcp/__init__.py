"""mcp-defime: dados de pools DeFi (Robinhood Chain e outras EVM) para agentes via MCP."""

from .client import PoolClient
from .config import Settings

__all__ = ["PoolClient", "Settings"]
__version__ = "0.1.0"
