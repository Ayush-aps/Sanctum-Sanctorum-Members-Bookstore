"""Library loan operations: borrowing and returning books."""
from datetime import datetime, timedelta
from math import ceil
from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Book, Loan, Member, MemberTier
from app.schemas import LoanCreate, LoanOut, LoanStatus

# Maximum concurrent unreturned loans per tier (None = unlimited).
TIER_LOAN_LIMIT: Dict[str, Optional[int]] = {
    MemberTier.APPRENTICE.value: 1,
    MemberTier.ADEPT.value: 3,
    MemberTier.MASTER.value: 5,
    MemberTier.SUPREME.value: None,
}

LOAN_PERIOD = timedelta(days=14)
LATE_FEE_PER_DAY_CENTS = 25


def loan_status(loan: Loan, now: datetime) -> LoanStatus:
    """``returned`` if returned; else ``overdue`` if now > due_at; else ``active``."""
    if loan.returned_at is not None:
        return "returned"
    if now > loan.due_at:
        return "overdue"
    return "active"
    


def to_loan_out(loan: Loan, now: datetime) -> LoanOut:
    """Serialize a loan, computing its status at read time."""
    return LoanOut(
        id=loan.id,
        member_id=loan.member_id,
        book_id=loan.book_id,
        borrowed_at=loan.borrowed_at,
        due_at=loan.due_at,
        returned_at=loan.returned_at,
        late_fee_cents=loan.late_fee_cents,
        status=loan_status(loan, now),
    )
    


def calculate_late_fee(due_at: datetime, returned_at: datetime, price_cents: int) -> int:
    """25 cents per started day late (any partial day counts), capped at the book's price; 0 if not late."""
    if returned_at <= due_at:
        return 0

    late_duration = returned_at - due_at
    # ceil(duration / 1 day):
    # 1 second late -> 1 day
    # 1 second + 23 hours late -> 1 day
    # exactly 1 day late -> 1 day
    # 1 day + 1 second late -> 2 days
    days_late = ceil(late_duration.total_seconds() / timedelta(days=1).total_seconds())

    fee = days_late * LATE_FEE_PER_DAY_CENTS

    return min(fee, price_cents)
    


def create_loan(db: Session, data: LoanCreate, now: datetime) -> LoanOut:
    """Borrow a book for 14 days.

    Checks, in order:
    1. 404 member not found; 404 book not found
    2. 403 book restricted and member tier below master
    3. 409 member has any overdue loan
    4. 409 member already has an unreturned loan of this book
    5. 409 member is at their tier's loan limit
    6. 409 book is out of stock
    On success: borrowed_at = now, due_at = now + 14 days, returned_at None,
    late_fee_cents 0, and stock is decremented by one.
    """
     # ------------------------------------------------------------------
    # 1. Required existence checks, in the exact order from the spec.
    # ------------------------------------------------------------------

    member = (
        db.execute(
            select(Member)
            .where(Member.id == data.member_id)
            .with_for_update()
        )
        .scalar_one_or_none()
    )

    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")

    book = (
        db.execute(
            select(Book)
            .where(Book.id == data.book_id)
            .with_for_update()
        )
        .scalar_one_or_none()
    )

    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    # ------------------------------------------------------------------
    # 2. Restricted-book access.
    # Master and Supreme are allowed.
    # Apprentice and Adept are below master.
    # ------------------------------------------------------------------

    if book.restricted and member.tier not in (
        MemberTier.MASTER.value,
        MemberTier.SUPREME.value,
    ):
        raise HTTPException(
            status_code=403,
            detail="Member tier does not allow borrowing this restricted book",
        )

    # ------------------------------------------------------------------
    # 3. Any overdue unreturned loan blocks a new loan.
    #
    # Strict boundary:
    # due_at == now is NOT overdue.
    # ------------------------------------------------------------------

    overdue_loan_id = db.scalar(
        select(Loan.id)
        .where(
            Loan.member_id == member.id,
            Loan.returned_at.is_(None),
            Loan.due_at < now,
        )
        .limit(1)
    )

    if overdue_loan_id is not None:
        raise HTTPException(
            status_code=409,
            detail="Member has an overdue loan",
        )

    # ------------------------------------------------------------------
    # 4. Same book may not be borrowed twice while an earlier loan
    # remains unreturned.
    # ------------------------------------------------------------------

    existing_book_loan_id = db.scalar(
        select(Loan.id)
        .where(
            Loan.member_id == member.id,
            Loan.book_id == book.id,
            Loan.returned_at.is_(None),
        )
        .limit(1)
    )

    if existing_book_loan_id is not None:
        raise HTTPException(
            status_code=409,
            detail="Member already has an unreturned loan for this book",
        )

    # ------------------------------------------------------------------
    # 5. Tier loan limit.
    # ------------------------------------------------------------------

    limit = TIER_LOAN_LIMIT[member.tier]

    if limit is not None:
        active_loan_count = (
            db.scalar(
                select(func.count(Loan.id)).where(
                    Loan.member_id == member.id,
                    Loan.returned_at.is_(None),
                )
            )
            or 0
        )

        if active_loan_count >= limit:
            raise HTTPException(
                status_code=409,
                detail="Member has reached the loan limit for their tier",
            )

    # ------------------------------------------------------------------
    # 6. Stock check.
    # ------------------------------------------------------------------

    if book.stock == 0:
        raise HTTPException(
            status_code=409,
            detail="Book is out of stock",
        )

    # ------------------------------------------------------------------
    # Perform the actual mutation.
    #
    # The stock decrement uses an atomic UPDATE condition so stock can
    # never become negative even if another writer changes it.
    # ------------------------------------------------------------------

    stock_update = db.execute(
        update(Book)
        .where(
            Book.id == book.id,
            Book.stock > 0,
        )
        .values(stock=Book.stock - 1)
    )

    if stock_update.rowcount != 1:
        # This can occur if another transaction consumed the last copy
        # between the earlier read and this update.
        raise HTTPException(
            status_code=409,
            detail="Book is out of stock",
        )

    loan = Loan(
        member_id=member.id,
        book_id=book.id,
        borrowed_at=now,
        due_at=now + LOAN_PERIOD,
        returned_at=None,
        late_fee_cents=0,
    )

    try:
        db.add(loan)

        # Flush gives us the generated loan id without committing yet.
        db.flush()

        result = to_loan_out(loan, now)

        # Loan creation and stock decrement are committed together.
        db.commit()

        return result

    except Exception:
        # Do not hide the original DB error.
        # Roll back both the loan insert and stock change atomically.
        db.rollback()
        raise



def get_loan(db: Session, loan_id: int, now: datetime) -> LoanOut:
    """Return a loan by id, or raise 404."""
    loan = db.get(Loan, loan_id)

    if loan is None:
        raise HTTPException(status_code=404, detail="Loan not found")

    return to_loan_out(loan, now)



def return_loan(db: Session, loan_id: int, now: datetime) -> LoanOut:
    """Return a borrowed book.

    Rules: 404 if missing; 409 if already returned. Sets returned_at = now, restores one copy
    of stock and charges a late fee (see ``calculate_late_fee``).
    """
     # Fetch loan + current book price together.
    # The current book price is deliberately used for the late-fee cap.
    row = db.execute(
        select(Loan, Book)
        .join(Book, Book.id == Loan.book_id)
        .where(Loan.id == loan_id)
        .with_for_update()
    ).first()

    if row is None:
        raise HTTPException(status_code=404, detail="Loan not found")

    loan, book = row

    if loan.returned_at is not None:
        raise HTTPException(
            status_code=409,
            detail="Loan has already been returned",
        )

    # Compute the fee before mutating the loan.
    late_fee = calculate_late_fee(
        due_at=loan.due_at,
        returned_at=now,
        price_cents=book.price_cents,
    )

    loan.returned_at = now
    loan.late_fee_cents = late_fee

    # Atomic stock increment.
    db.execute(
        update(Book)
        .where(Book.id == book.id)
        .values(stock=Book.stock + 1)
    )

    try:
        db.flush()

        result = to_loan_out(loan, now)

        # Loan return + late fee + stock restoration are one transaction.
        db.commit()

        return result

    except Exception:
        # Preserve the original exception while ensuring no half-update.
        db.rollback()
        raise



def list_member_loans(
    db: Session, member_id: int, now: datetime, status: Optional[LoanStatus] = None
) -> List[LoanOut]:
    """A member's loans ordered by id, optionally filtered by computed status; 404 if member missing."""
    member = db.get(Member, member_id)

    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")

    stmt = (
        select(Loan)
        .where(Loan.member_id == member_id)
        .order_by(Loan.id.asc())
    )

    # Push status filtering into SQL rather than loading every loan and
    # filtering everything in Python.

    if status == "active":
        stmt = stmt.where(
            Loan.returned_at.is_(None),
            Loan.due_at >= now,
        )

    elif status == "overdue":
        stmt = stmt.where(
            Loan.returned_at.is_(None),
            Loan.due_at < now,
        )

    elif status == "returned":
        stmt = stmt.where(
            Loan.returned_at.is_not(None),
        )

    loans = db.scalars(stmt).all()

    return [to_loan_out(loan, now) for loan in loans]