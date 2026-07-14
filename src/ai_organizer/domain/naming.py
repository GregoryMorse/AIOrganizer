from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

INVALID_WINDOWS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(slots=True)
class NamingProfile:
    id: str
    name: str
    template: str
    separator: str = " - "
    date_format: str = "%Y-%m-%d"
    max_component_length: int = 180
    aliases: dict[str, str] = field(default_factory=dict)
    abbreviations: dict[str, str] = field(default_factory=dict)
    case_style: str = "preserve"
    unicode_form: Literal["NFC", "NFD", "NFKC", "NFKD"] = "NFC"
    collision_suffix: str = " - {counter:02d}"
    revision: int = 1

    def render(self, tokens: Mapping[str, str | datetime | None], extension: str) -> str:
        normalized: dict[str, str] = {}
        for key, value in tokens.items():
            if value is None:
                normalized[key] = ""
            elif isinstance(value, datetime):
                normalized[key] = value.strftime(self.date_format)
            else:
                text = str(value).strip()
                normalized[key] = self.aliases.get(text.casefold(), text)
                normalized[key] = self.abbreviations.get(
                    normalized[key].casefold(), normalized[key]
                )
                normalized[key] = _apply_case(normalized[key], self.case_style)
        rendered = self.template.format_map(_MissingTokenMap(normalized))
        rendered = re.sub(r"\s+", " ", rendered).strip(" .-_\t")
        rendered = re.sub(r"(?:\s+-\s+){2,}", self.separator, rendered)
        rendered = unicodedata.normalize(self.unicode_form, rendered)
        rendered = INVALID_WINDOWS.sub("-", rendered).rstrip(" .")
        if not rendered:
            raise ValueError("Naming profile produced an empty filename")
        if rendered.upper() in RESERVED_WINDOWS:
            rendered = f"_{rendered}"
        budget = max(1, self.max_component_length - len(extension) - 1)
        rendered = rendered[:budget].rstrip(" .-_")
        return f"{rendered}.{extension.lstrip('.')}" if extension else rendered


class _MissingTokenMap(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def builtin_naming_profiles() -> list[NamingProfile]:
    return [
        NamingProfile(
            "readable-document",
            "Readable Document",
            "{date} - {entity} - {document_type} - {descriptor} - {period}",
        ),
        NamingProfile(
            "compact-hierarchy",
            "Compact Hierarchy",
            "{date_compact}{hierarchy}{semantic_description}",
            separator="",
            date_format="%Y%m%d",
        ),
        NamingProfile("minimal-clean", "Minimal Clean", "{date} - {title}"),
        NamingProfile(
            "media-capture",
            "Media Capture",
            "{datetime} - {location} - {description}",
            date_format="%Y-%m-%d %H.%M.%S",
        ),
        NamingProfile("preserve-correct", "Preserve and Correct", "{clean_title}"),
    ]


def disambiguate(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    output: list[str] = []
    for name in names:
        key = unicodedata.normalize("NFC", name).casefold()
        count = seen.get(key, 0) + 1
        seen[key] = count
        if count == 1:
            output.append(name)
            continue
        path = Path(name)
        output.append(f"{path.stem} - {count:02d}{path.suffix}")
    return output


def _apply_case(value: str, style: str) -> str:
    match style:
        case "lower":
            return value.lower()
        case "upper":
            return value.upper()
        case "title":
            return value.title()
        case "snake":
            return re.sub(r"\s+", "_", value.lower())
        case "kebab":
            return re.sub(r"\s+", "-", value.lower())
        case _:
            return value
