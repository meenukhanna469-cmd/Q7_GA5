"""
schemas.py — the safety net. The LLM's output is NEVER trusted directly.
Every field is re-validated here before anything is persisted or acted on.
"""

ALLOWED_ACTIONS = {
    "create_draft",
    "update_internal_record",
    "send_approved_notice",
    "request_confirmation",
    "quarantine_item",
    "no_action",
}

# ASSUMPTION: required payload fields per action. Tighten/rename these to match
# your dossier data model once you know it.
REQUIRED_FIELDS = {
    "create_draft": {"queue", "recipient", "body"},
    "update_internal_record": {"record_id", "field", "new_value"},
    "send_approved_notice": {"recipient", "template", "approval_ref"},
    "request_confirmation": {"queue", "reason"},
    "quarantine_item": {"reason"},
    "no_action": {"reason"},
}


class SchemaError(Exception):
    pass


def validate_decision(decision: dict, dossier: dict):
    """
    decision: {"action": str, "target": str|None, "payload": dict, "evidence": [str,...]}
    Raises SchemaError on any violation. Returns the (possibly normalized) decision.
    """
    action = decision.get("action")
    if action not in ALLOWED_ACTIONS:
        raise SchemaError(f"unknown action: {action}")

    payload = decision.get("payload") or {}
    if not isinstance(payload, dict):
        raise SchemaError("payload must be an object")

    required = REQUIRED_FIELDS[action]
    missing = required - set(payload.keys())
    if missing:
        raise SchemaError(f"missing payload fields for {action}: {missing}")

    evidence = decision.get("evidence") or []
    if not isinstance(evidence, list) or not evidence:
        raise SchemaError("evidence must be a non-empty list of quoted lines")

    # --- Hard safety checks (pure code, cannot be talked around by the model) ---

    if action == "send_approved_notice":
        # Outbound sends require a real, code-verifiable approval reference that
        # exists in the TRUSTED/internal part of the dossier -- never accept an
        # approval the model merely "noticed" in the untrusted body text.
        trusted_approvals = dossier.get("trusted_metadata", {}).get("approvals", [])
        approval_ref = payload.get("approval_ref")
        if approval_ref not in trusted_approvals:
            raise SchemaError(
                "send_approved_notice rejected: approval_ref not present in trusted_metadata.approvals"
            )
        # scope check: recipient/template must match what was approved, not just asserted
        # (extend this once you know the exact shape of an "approval" record)

    if action in ("create_draft", "update_internal_record"):
        # never let raw untrusted dossier text get treated as a tool argument verbatim
        # beyond what's needed -- e.g. reject payloads that look like they're smuggling
        # instructions rather than data (very defensive heuristic; refine as needed)
        for v in payload.values():
            if isinstance(v, str) and len(v) > 4000:
                raise SchemaError("payload field suspiciously large -- possible raw content injection")

    return {
        "action": action,
        "target": decision.get("target"),
        "payload": payload,
        "evidence": evidence,
    }
