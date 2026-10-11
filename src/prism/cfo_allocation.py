"""Offline decision for a CFO.ai budget snapshot; persistence belongs to the MCP client."""

from decimal import Decimal, InvalidOperation


def decide(request: dict, budget: dict | None, policy: dict | None, allocations: list[dict]) -> tuple[dict, dict | None]:
    """Return (structured decision, new reservation row); reuse an existing request ID.

    The budget snapshot's existing_reservations_usd includes all allocation rows.
    """
    request_id = request["request_id"]
    if not request_id or not request["repository"]:
        raise ValueError("request_id and repository are required")
    matches = [row for row in allocations if row["request_id"] == request_id]
    if len(matches) > 1:
        return _blocked("duplicate request ID records"), None
    if matches:
        row = matches[0]
        identity = ("repository", "issue_url", "pinned_commit", "model", "estimated_usd")
        if any(key not in row or key not in request or row[key] != request[key] for key in identity):
            return _blocked("request ID already belongs to different inputs"), None
        return {key: row[key] for key in ("decision", "allocation_id", "allocated_usd_cap", "available_balance", "reason")}, None
    if not budget or not policy:
        return _blocked("repository budget or allocation policy is missing"), None
    if budget.get("repository") != request["repository"]:
        return _blocked("repository budget is missing"), None
    try:
        amount = _usd(request["estimated_usd"])
        ceiling = _usd(budget["budget_usd"])
        spent = _usd(budget["spending_usd"])
        opening = _usd(budget["existing_reservations_usd"])
        fraction = Decimal(str(policy["max_budget_fraction"]))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return _blocked("budget, policy, or reservation data is invalid"), None
    if not fraction.is_finite() or not 0 < fraction <= 1:
        return _blocked("allocation policy fraction is invalid"), None
    available = ceiling - spent - opening
    if amount > available or amount > ceiling * fraction:
        return {"decision": "denied", "allocation_id": None, "allocated_usd_cap": 0,
                "available_balance": float(available), "reason": "estimate exceeds available balance or policy cap"}, None
    result = {"decision": "allocated", "allocation_id": f"SYNTHETIC/prism-test-allocation-{request_id}",
              "allocated_usd_cap": float(amount), "available_balance": float(available - amount),
              "reason": "central end-to-end estimate is within available balance and policy cap"}
    # shortcut: read-before-write is not atomic; use a conditional insert if CFO.ai exposes one.
    return result, dict(request, **result, reservation_usd=float(amount))


def _usd(value) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("USD amounts must be finite and nonnegative")
    return amount


def _blocked(reason: str) -> dict:
    return {"decision": "blocked", "allocation_id": None, "allocated_usd_cap": None,
            "available_balance": None, "reason": reason}
