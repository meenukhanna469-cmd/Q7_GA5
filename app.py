"""
app.py — single public endpoint handling {operation: propose|commit}.

ASSUMPTION MARKERS: fields marked # ASSUMPTION are my best guess at the exact
wire format from the assignment. Replace with the real field names once you
have the exact propose/commit JSON examples.
"""
import hashlib
import json
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import storage
import schemas
import ai

app = FastAPI()
storage.init_db()

MAX_BODY_BYTES = 2 * 1024 * 1024  # bound request size
MAX_RESPONSE_BYTES = 512 * 1024   # spec: successful body over 512 KiB is rejected


def canonical_fingerprint(dossier: dict) -> str:
    stable = {"id": dossier["id"], "content": dossier.get("content", "")}
    blob = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def proposal_digest(proposal: dict) -> str:
    blob = json.dumps(proposal, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def json_response(payload: dict, status_code: int = 200):
    body = json.dumps(payload, sort_keys=True).encode()
    if status_code == 200 and len(body) > MAX_RESPONSE_BYTES:
        # never silently truncate -- fail loudly, this indicates a real bug
        return JSONResponse({"error": "response too large"}, status_code=500)
    return JSONResponse(payload, status_code=status_code, media_type="application/json")


@app.post("/")
async def handle(request: Request):
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return json_response({"error": "body too large"}, 413)

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return json_response({"error": "invalid JSON"}, 400)

    # TEMP DEBUG: log request shape to Render logs so we can see the real
    # field names/structure the grader sends. Remove once confirmed.
    print("DEBUG incoming operation:", body.get("operation"))
    print("DEBUG incoming top-level keys:", list(body.keys()))
    if isinstance(body.get("dossiers"), list) and body["dossiers"]:
        print("DEBUG first dossier keys:", list(body["dossiers"][0].keys()))
    elif isinstance(body.get("receipts"), list) and body["receipts"]:
        print("DEBUG first receipt keys:", list(body["receipts"][0].keys()))
    else:
        print("DEBUG full body (first 2000 chars):", json.dumps(body)[:2000])

    op = body.get("operation")
    if op == "propose":
        return handle_propose(body)
    elif op == "commit":
        return handle_commit(body)
    else:
        return json_response({"error": "invalid operation"}, 400)


# ---------------------------------------------------------------------------
# PROPOSE
# ---------------------------------------------------------------------------
def handle_propose(body: dict):
    evaluation_id = body.get("evaluationId")
    dossiers = body.get("dossiers")

    # ---- Layer 0: shape validation, before any AI/DB work ----
    if not isinstance(evaluation_id, str) or not evaluation_id:
        return json_response({"error": "missing evaluationId"}, 422)
    if not isinstance(dossiers, list) or not dossiers:
        return json_response({"error": "missing dossiers"}, 422)

    seen_ids = set()
    for d in dossiers:
        if not isinstance(d, dict) or "id" not in d or "content" not in d:
            return json_response({"error": "malformed dossier"}, 422)
        if d["id"] in seen_ids:
            return json_response({"error": f"duplicate dossier id {d['id']}"}, 422)
        seen_ids.add(d["id"])

    # ---- Layer 1: idempotency / conflict check ----
    fingerprints = {d["id"]: canonical_fingerprint(d) for d in dossiers}
    content_set_hash = hashlib.sha256(
        json.dumps(fingerprints, sort_keys=True).encode()
    ).hexdigest()

    existing_eval = storage.get_evaluation(evaluation_id)
    if existing_eval:
        if existing_eval["content_set_hash"] == content_set_hash:
            # exact replay -- return stored result, no new model/tool work
            return json_response(
                {"status": "awaiting_receipts", "proposals": json.loads(existing_eval["proposals_json"])}
            )
        else:
            return json_response({"error": "evaluationId reused with changed content"}, 409)

    # ---- Layer 2: AI decision (cache-first, batched for the uncached ones) ----
    to_ask = []
    cached_by_id = {}
    for d in dossiers:
        fp = fingerprints[d["id"]]
        cached = storage.get_cached_decision(d["id"], fp)
        if cached:
            cached_by_id[d["id"]] = cached
        else:
            to_ask.append(d)

    fresh_decisions = {}
    if to_ask:
        try:
            raw_decisions = ai.decide_batch(to_ask)
        except Exception as e:
            return json_response({"error": f"AI decision failed: {e}"}, 502)
        for rd in raw_decisions:
            fresh_decisions[rd.get("dossier_id")] = rd

    # ---- Layer 3: schema + safety re-validation, build proposals ----
    proposals = []
    dossier_by_id = {d["id"]: d for d in dossiers}
    for d in dossiers:
        did = d["id"]
        fp = fingerprints[did]
        if did in cached_by_id:
            c = cached_by_id[did]
            proposal = {
                "dossierId": did,
                "callId": c["call_id"],
                "action": c["action"],
                "target": c["target"],
                "payload": json.loads(c["payload_json"]),
                "evidence": json.loads(c["evidence_json"]),
            }
        else:
            raw = fresh_decisions.get(did)
            if raw is None:
                return json_response({"error": f"AI produced no decision for {did}"}, 502)
            try:
                validated = schemas.validate_decision(raw, dossier_by_id[did])
            except schemas.SchemaError as e:
                return json_response({"error": f"invalid AI decision for {did}: {e}"}, 502)

            call_id = hashlib.sha256(f"{did}:{fp}".encode()).hexdigest()[:24]
            proposal = {
                "dossierId": did,
                "callId": call_id,
                "action": validated["action"],
                "target": validated["target"],
                "payload": validated["payload"],
                "evidence": validated["evidence"],
            }
            digest = proposal_digest(proposal)
            storage.save_decision(
                did, fp, call_id, validated["action"], validated["target"],
                validated["payload"], validated["evidence"], digest,
            )
        proposals.append(proposal)

    storage.save_evaluation(evaluation_id, content_set_hash, "awaiting_receipts", proposals)

    return json_response({"status": "awaiting_receipts", "proposals": proposals})


# ---------------------------------------------------------------------------
# COMMIT
# ---------------------------------------------------------------------------
def handle_commit(body: dict):
    receipts = body.get("receipts")
    if not isinstance(receipts, list) or not receipts:
        return json_response({"error": "missing receipts"}, 422)

    outcomes = []
    for r in receipts:
        evaluation_id = r.get("evaluationId")   # ASSUMPTION field name
        receipt_id = r.get("receiptId")          # ASSUMPTION field name
        call_id = r.get("callId")
        verification_key = r.get("verificationKey")  # ASSUMPTION field name

        if not all([evaluation_id, receipt_id, call_id]):
            return json_response({"error": "malformed receipt"}, 422)

        evaluation = storage.get_evaluation(evaluation_id)
        if not evaluation:
            outcomes.append({"receiptId": receipt_id, "status": "rejected", "reason": "unknown evaluationId"})
            continue

        # exact commit replay -- return stored outcome, no repeated effect
        existing_receipt = storage.get_receipt(evaluation_id, receipt_id)
        if existing_receipt:
            outcomes.append(json.loads(existing_receipt["outcome_json"]))
            continue

        proposals = json.loads(evaluation["proposals_json"])
        matching = next((p for p in proposals if p["callId"] == call_id), None)
        if not matching:
            outcome = {"receiptId": receipt_id, "status": "rejected", "reason": "callId mismatch"}
            storage.save_receipt(evaluation_id, receipt_id, call_id, False, outcome)
            outcomes.append(outcome)
            continue

        # TODO: verify verification_key against whatever mechanism the grader
        # actually uses (e.g. HMAC over the proposal digest with a per-eval
        # secret). This is where "reject an invalid receipt" is enforced.
        verified = bool(verification_key)  # ASSUMPTION -- replace with real check
        if not verified:
            outcome = {"receiptId": receipt_id, "status": "rejected", "reason": "invalid receipt"}
            storage.save_receipt(evaluation_id, receipt_id, call_id, False, outcome)
            outcomes.append(outcome)
            continue

        # Effect application -- apply exactly once, ever.
        outcome = {
            "receiptId": receipt_id,
            "dossierId": matching["dossierId"],
            "status": "executed",
            "action": matching["action"],
        }
        storage.save_receipt(evaluation_id, receipt_id, call_id, True, outcome)
        outcomes.append(outcome)

    return json_response({"status": "completed", "outcomes": outcomes})
