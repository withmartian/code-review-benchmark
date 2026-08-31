"""Pluggable parser framework for tool-specific comment parsing."""

from __future__ import annotations

from code_review_benchmark.parsers.base import BaseParser
from code_review_benchmark.parsers.base import ParsedComment
from code_review_benchmark.parsers.base import ParsedReview

__all__ = ["BaseParser", "ParsedComment", "ParsedReview", "get_parser"]

# Registry — populated lazily on first get_parser() call
PARSERS: dict[str, type[BaseParser]] = {}
_registered = False


def _register_parsers() -> None:
    global _registered
    if _registered:
        return
    _registered = True
    try:
        from code_review_benchmark.parsers.coderabbit import CodeRabbitParser

        PARSERS["coderabbit"] = CodeRabbitParser
    except ImportError:
        pass  # CodeRabbitParser not yet created


def get_parser(tool: str) -> BaseParser:
    """Return the parser for a given tool, or DefaultParser if none registered."""
    from code_review_benchmark.parsers.default import DefaultParser

    _register_parsers()
    return PARSERS.get(tool, DefaultParser)()
