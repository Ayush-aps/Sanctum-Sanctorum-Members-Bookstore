"""Member operations and tier helpers."""
from datetime import datetime
from typing import List

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Loan, Member, MemberTier, Order, OrderStatus
from app.schemas import MemberCreate, MemberStats

# Tiers from lowest to highest; a member's rank is their index in this list.
TIER_ORDER: List[str] = [
    MemberTier.APPRENTICE.value,
    MemberTier.ADEPT.value,
    MemberTier.MASTER.value,
    MemberTier.SUPREME.value,
]

# Minimum tier allowed to buy or borrow restricted books.
RESTRICTED_MIN_TIER = MemberTier.MASTER.value


def tier_at_least(tier: str, minimum: str) -> bool:
    """True if ``tier`` ranks at or above ``minimum``."""
    return TIER_ORDER.index(tier) >= TIER_ORDER.index(minimum)


def ensure_can_access_restricted(member: Member) -> None:
    """Raise 403 unless the member's tier may access restricted books."""
    if not tier_at_least(member.tier, RESTRICTED_MIN_TIER):
        raise HTTPException(
            status_code=403, detail=f"Restricted books require tier '{RESTRICTED_MIN_TIER}' or higher"
        )


def create_member(db: Session, data: MemberCreate, now: datetime) -> Member:
    """Register a member.

    Rules: email (already stripped + lowercased) must be unique -> 409; created_at = now.
    """
    # TODO: reject an email that is already in use with 409


    # The schema normalizes email, but keeping the comparison
    # case-insensitive here protects the business rule even when
    # this service is called directly.
    normalized_email = data.email.strip().lower()

    # Fast path for the normal duplicate case.
    existing_member = db.scalar(
        select(Member.id)
        .where(func.lower(Member.email) == normalized_email)
        .limit(1)
    )

    if existing_member is not None:
        raise HTTPException(
            status_code=409,
            detail="Email is already in use",
        )


    member = Member(name=data.name, email=normalized_email, tier=data.tier.value, created_at=now)
    db.add(member)

    try:
        db.commit()
    except IntegrityError:
        # The UNIQUE constraint is the final protection against
        # two concurrent requests creating the same email.
        db.rollback()

        conflicting_member = db.scalar(
            select(Member.id)
            .where(func.lower(Member.email) == normalized_email)
            .limit(1)
        )

        if conflicting_member is not None:
            raise HTTPException(
                status_code=409,
                detail="Email is already in use",
            )

        # Do not hide unrelated database integrity errors.
        raise
    
    db.refresh(member)
    return member


def get_member(db: Session, member_id: int) -> Member:
    """Return a member by id, or raise 404."""
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return member


def list_member_orders(db: Session, member_id: int) -> List[Order]:
    """All orders of a member ordered by id ascending; 404 if the member is missing."""
    get_member(db, member_id)
    return list(db.scalars(select(Order).where(Order.member_id == member_id).order_by(Order.id.asc))) # here i added .asc at the end


def get_member_stats(db: Session, member_id: int, now: datetime) -> MemberStats:
    """Summarize a member's activity.

    Rules:
    - 404 if the member is missing.
    - orders_paid / total_spent_cents consider only ``paid`` orders.
    - active_loans counts every unreturned loan (overdue ones included).
    - overdue_loans counts unreturned loans with now > due_at.
    - late_fees_cents sums late fees of returned loans.
    """
    raise NotImplementedError("get_member_stats")
