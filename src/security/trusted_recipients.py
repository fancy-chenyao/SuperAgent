"""Platform-owned recipient resolution for remote email authorization."""

from __future__ import annotations

import json
import unicodedata
from typing import Any, Iterable

from src.utils.path_utils import get_project_root


class TrustedRecipientResolutionError(ValueError):
    """A semantic recipient could not be resolved without expanding access."""


class AmbiguousTrustedRecipientError(TrustedRecipientResolutionError):
    """A recipient label maps to more than one trusted mailbox."""


def _normalized(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _recipient_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").replace(";", ",").split(",") if part.strip()]


def _trusted_directory() -> list[dict[str, Any]]:
    root = get_project_root() / "assets"
    entries: list[dict[str, Any]] = []
    contacts_path = root / "contacts.json"
    if contacts_path.exists():
        data = json.loads(contacts_path.read_text(encoding="utf-8-sig"))
        entries.extend(item for item in data.get("contacts", []) if isinstance(item, dict))
    people_path = root / "person_info_sample.json"
    if people_path.exists():
        data = json.loads(people_path.read_text(encoding="utf-8-sig"))
        for person in data.get("personInfoList", []):
            if not isinstance(person, dict):
                continue
            entries.append({
                "name": person.get("adtEmpeNm"),
                "position": person.get("tcoPostNm") or person.get("nwgntPstNm"),
                "alternate_position": person.get("nwgntPstNm"),
                "email": person.get("internalMaiBox"),
            })
    return entries


def resolve_trusted_recipient_addresses(recipients: Any) -> list[str]:
    """Resolve each recipient to exactly one platform-controlled mailbox.

    Exact directory email addresses are valid identities.  Semantic labels
    (name or position) must resolve uniquely; returning every match for an
    ambiguous title would silently widen the send authorization.
    """

    requested = [item for item in _recipient_values(recipients) if _normalized(item)]
    if not requested:
        return []
    directory = _trusted_directory()
    resolved: set[str] = set()
    for raw_recipient in requested:
        recipient = _normalized(raw_recipient)
        email_matches = {
            str(entry.get("email") or "").strip()
            for entry in directory
            if _normalized(entry.get("email")) == recipient
            and str(entry.get("email") or "").strip()
        }
        matches = email_matches
        if not matches:
            matches = {
                str(entry.get("email") or "").strip()
                for entry in directory
                if str(entry.get("email") or "").strip()
                and recipient
                in {
                    _normalized(entry.get("name")),
                    _normalized(entry.get("position")),
                    _normalized(entry.get("alternate_position")),
                }
            }
        if len(matches) > 1:
            raise AmbiguousTrustedRecipientError(
                f"trusted recipient is ambiguous: {raw_recipient!r} matches "
                f"{len(matches)} mailboxes"
            )
        resolved.update(matches)
    return sorted(resolved, key=str.casefold)
