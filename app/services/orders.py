"""Order operations: placing, paying and cancelling purchases."""
from datetime import datetime
from typing import Dict

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.models import Book, Member, MemberTier, Order, OrderItem, OrderStatus
from app.schemas import OrderCreate
from app.services.members import ensure_can_access_restricted

# Percentage discount granted by each membership tier.
TIER_DISCOUNT_PERCENT: Dict[str, int] = {
    MemberTier.APPRENTICE.value: 0,
    MemberTier.ADEPT.value: 5,
    MemberTier.MASTER.value: 10,
    MemberTier.SUPREME.value: 15,
}

# Extra discount when the total quantity across all items reaches the threshold.
BULK_QUANTITY_THRESHOLD = 10
BULK_DISCOUNT_PERCENT = 5


def calculate_discount_percent(member: Member, total_quantity: int) -> int:
    """Tier discount, plus the bulk discount when total quantity >= threshold."""

    tier_discount = TIER_DISCOUNT_PERCENT[member.tier]

    bulk_discount = (
        BULK_DISCOUNT_PERCENT
        if total_quantity >= BULK_QUANTITY_THRESHOLD
        else 0
    )

    return tier_discount + bulk_discount

    # raise NotImplementedError("calculate_discount_percent")


# both below functions are small private helper functions to improve the architecture and avoid duplicated database/query logic.
def _load_books(db: Session, book_ids: list[int]) -> Dict[int, Book]:
    """Load all requested books in one query and index them by id."""

    books = db.scalars(
        select(Book)
        .where(Book.id.in_(book_ids))
        .with_for_update()
    ).all()

    return {book.id: book for book in books}


def _load_order(db: Session, order_id: int) -> Order:
    """Load an order and its items, or raise 404."""

    order = db.scalar(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_id)
    )

    if order is None:
        raise HTTPException(
            status_code=404,
            detail="Order not found",
        )

    return order



def create_order(db: Session, data: OrderCreate, now: datetime) -> Order:
    """Place a pending order and reserve stock.

    Checks, in order (422 for empty items / bad quantity / duplicate books is done by the schema):
    1. 404 member not found; 404 any book not found
    2. 403 any book restricted and member tier below master
    3. 409 any book has insufficient stock (all-or-nothing: nothing is changed)
    Then stock is decremented for every item and prices are snapshotted.
    Pricing: discount_cents = subtotal * percent // 100; total = subtotal - discount.
    """
    # TODO:
    # 1. Load the member (404) and every book (404).
    # 2. If any book is restricted, check the member's tier (403).
    # 3. Check stock for every item before changing anything (409).
    # 4. Decrement stock and build OrderItems with the current price as unit_price_cents.
    # 5. Compute subtotal, discount_percent (calculate_discount_percent), discount_cents, total.
    # 6. Save the pending Order with created_at = now and return it.

    """Validation handled by the schema:
    - items must not be empty
    - every quantity must be >= 1
    - a book may appear only once
    - Stock reservation is all-or-nothing."""

    try:
        # ------------------------------------------------------------------
        # 1. Load member
        # ------------------------------------------------------------------
        member = db.get(Member, data.member_id)

        if member is None:
            raise HTTPException(
                status_code=404,
                detail="Member not found",
            )

        # ------------------------------------------------------------------
        # 2. Load every requested book in one query
        # ------------------------------------------------------------------
        book_ids = [item.book_id for item in data.items]
        books_by_id = _load_books(db, book_ids)

        # Check missing books before any restricted/stock checks.
        for book_id in book_ids:
            if book_id not in books_by_id:
                raise HTTPException(
                    status_code=404,
                    detail=f"Book {book_id} not found",
                )

        # ------------------------------------------------------------------
        # 3. Restricted-book access check
        # ------------------------------------------------------------------
        if any(book.restricted for book in books_by_id.values()):
            ensure_can_access_restricted(member)

        # ------------------------------------------------------------------
        # 4. Check stock for every item BEFORE changing anything
        # ------------------------------------------------------------------
        for item in data.items:
            book = books_by_id[item.book_id]

            if book.stock < item.quantity:
                raise HTTPException(
                    status_code=409,
                    detail=f"Insufficient stock for book {book.id}",
                )

        # ------------------------------------------------------------------
        # 5. Reserve stock atomically and calculate pricing
        # ------------------------------------------------------------------
        subtotal_cents = 0
        total_quantity = 0

        for item in data.items:
            book = books_by_id[item.book_id]

            # Atomic stock reservation:
            # the database only decrements if enough stock still exists.
            result = db.execute(
                update(Book)
                .where(
                    Book.id == book.id,
                    Book.stock >= item.quantity,
                )
                .values(
                    stock=Book.stock - item.quantity,
                )
            )

            # Another concurrent order may have consumed stock after the
            # initial check. A failed reservation aborts the whole order.
            if result.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail=f"Insufficient stock for book {book.id}",
                )

            subtotal_cents += book.price_cents * item.quantity
            total_quantity += item.quantity

        discount_percent = calculate_discount_percent(
            member,
            total_quantity,
        )

        # Integer arithmetic gives the required floor behavior.
        discount_cents = (
            subtotal_cents * discount_percent
        ) // 100

        total_cents = subtotal_cents - discount_cents

        # ------------------------------------------------------------------
        # 6. Create the order with price snapshots
        # ------------------------------------------------------------------
        order = Order(
            member_id=member.id,
            status=OrderStatus.PENDING.value,
            subtotal_cents=subtotal_cents,
            discount_percent=discount_percent,
            discount_cents=discount_cents,
            total_cents=total_cents,
            created_at=now,
            items=[
                OrderItem(
                    book_id=item.book_id,
                    quantity=item.quantity,
                    unit_price_cents=books_by_id[item.book_id].price_cents,
                )
                for item in data.items
            ],
        )

        db.add(order)

        # Flush assigns order/item IDs before commit.
        db.flush()

        # One transaction covers:
        # stock reservation + order creation + order items.
        db.commit()
        db.refresh(order)

        return order

    except Exception:
        # Critical for all-or-nothing behavior.
        #
        # If any stock reservation succeeds but a later item fails,
        # rollback restores all previous stock updates and prevents the
        # order from being created.
        db.rollback()
        raise


    # raise NotImplementedError("create_order")



def get_order(db: Session, order_id: int) -> Order:
    """Return an order by id, or raise 404."""

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def pay_order(db: Session, order_id: int) -> Order:
    """Mark a pending order as paid. 404 if missing; 409 if not pending.
    Missing order -> 404
    Non-pending order -> 409
    Reserved stock remains unchanged.
    """

    try:
        # Conditional update prevents an already-paid/cancelled order
        # from being transitioned again.
        result = db.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status == OrderStatus.PENDING.value,
            )
            .values(
                status=OrderStatus.PAID.value,
            )
        )

        if result.rowcount == 0:
            existing = db.get(Order, order_id)

            if existing is None:
                raise HTTPException(
                    status_code=404,
                    detail="Order not found",
                )

            raise HTTPException(
                status_code=409,
                detail=f"Cannot pay an order that is {existing.status}",
            )

        order = _load_order(db, order_id)

        db.commit()

        return order

    except Exception:
        db.rollback()
        raise
 


def cancel_order(db: Session, order_id: int) -> Order:
    """Cancel a pending order and restore the reserved stock. 404 if missing; 409 if not pending.
    Missing order -> 404
    Non-pending order -> 409
    Pending order -> cancelled + reserved stock restored.
    """

    try:
        # Change the status only if the order is still pending.
        result = db.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status == OrderStatus.PENDING.value,
            )
            .values(
                status=OrderStatus.CANCELLED.value,
            )
        )

        if result.rowcount == 0:
            existing = db.get(Order, order_id)

            if existing is None:
                raise HTTPException(
                    status_code=404,
                    detail="Order not found",
                )

            raise HTTPException(
                status_code=409,
                detail=f"Cannot cancel an order that is {existing.status}",
            )

        # Load the items for restoring the exact reserved quantities.
        order = _load_order(db, order_id)

        # Restore the reserved stock for every item.
        for item in order.items:
            db.execute(
                update(Book)
                .where(Book.id == item.book_id)
                .values(
                    stock=Book.stock + item.quantity,
                )
            )

        db.commit()

        return order

    except Exception:
        db.rollback()
        raise

