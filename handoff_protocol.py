"""Shared file-backed handoff protocol used by desktop and web workbenches."""

HANDOFF_MIN_LENGTH = 20
HANDOFF_MAX_LENGTH = 6000
HANDOFF_END = "[[END_HANDOFF]]"
HANDOFF_DONE = "[[COMMANDDOCK_DONE]]"
APPROVED = "[[APPROVED]]"

def handoff_marker(role_name: str) -> str:
    normalized = role_name.strip().upper().replace(" ", "_")
    return f"[[HANDOFF_TO_{normalized}]]"


def extract_latest_valid_handoff(buffer: str, marker: str) -> str:
    candidates = extract_handoff_candidates(buffer, marker)
    for _start, body, between_end_and_done in reversed(candidates):
        if len(between_end_and_done) > 80:
            continue
        return body
    return ""


def extract_handoff_candidates(buffer: str, marker: str) -> list[tuple[int, str, str]]:
    candidates: list[tuple[int, str, str]] = []
    search_from = 0
    while search_from < len(buffer):
        start = buffer.find(marker, search_from)
        if start == -1:
            break
        body_start = start + len(marker)
        end = buffer.find(HANDOFF_END, body_start)
        if end == -1:
            break
        done = buffer.find(HANDOFF_DONE, end + len(HANDOFF_END))
        if done == -1:
            search_from = end + len(HANDOFF_END)
            continue
        between_end_and_done = buffer[end + len(HANDOFF_END):done].strip()
        body = buffer[body_start:end].strip()
        candidates.append((start, body, between_end_and_done))
        search_from = done + len(HANDOFF_DONE)
    return candidates


def handoff_rejection_reason(body: str) -> str:
    trimmed = body.strip()
    if len(trimmed) < HANDOFF_MIN_LENGTH:
        return f"too-short:{len(trimmed)}"
    if len(trimmed) > HANDOFF_MAX_LENGTH:
        return f"too-long:{len(trimmed)}"
    if trimmed.lower() == "and":
        return "template-fragment"
    if "Direct message to the implementer:\nFirst stopping point:\nEvidence/tests expected:" in trimmed:
        return "placeholder-template"
    return ""


def has_approved_done(buffer: str) -> bool:
    return APPROVED in buffer and HANDOFF_DONE in buffer[buffer.rfind(APPROVED):]


