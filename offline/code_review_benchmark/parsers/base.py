"""Base classes for the parser framework."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field


@dataclass
class ParsedComment:
    """A single parsed comment with section classification and metadata."""

    body: str
    path: str | None
    line: int | None
    created_at: str
    section: str
    severity: str | None

    def to_dict(self) -> dict:
        return {
            "body": self.body,
            "path": self.path,
            "line": self.line,
            "created_at": self.created_at,
            "section": self.section,
            "severity": self.severity,
        }


@dataclass
class ParsedReview:
    """A parsed review containing comments categorized by section."""

    sections: dict[str, list[ParsedComment]] = field(default_factory=dict)

    def to_markdown(self) -> str:
        """Render all comments as clean markdown for LLM consumption."""
        parts: list[str] = []
        for _section_name, comments in self.sections.items():
            for comment in comments:
                header = _format_header(comment)
                parts.append(f"{header}\n\n{comment.body}")
        return "\n\n---\n\n".join(parts)


def _format_header(comment: ParsedComment) -> str:
    """Format a markdown header for a comment."""
    section_label = comment.section.replace("_", " ").title()
    if comment.path and comment.line is not None:
        location = f"{comment.path}:{comment.line}"
        if comment.severity:
            return f"## {section_label} — {location} [{comment.severity}]"
        return f"## {section_label} — {location}"
    if comment.severity:
        return f"## {section_label} [{comment.severity}]"
    return f"## {section_label}"


class BaseParser(ABC):
    """Abstract base for tool-specific comment parsers."""

    @abstractmethod
    def parse(self, review_comments: list[dict]) -> ParsedReview:
        """Parse raw review comments into categorized sections."""
        ...

    @abstractmethod
    def default_sections(self) -> dict[str, bool]:
        """Section names this parser produces and their default include/exclude.

        Keys are all section names the parser can emit.
        Values are True (included by default) or False (excluded by default).
        """
        ...
