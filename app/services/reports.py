"""Reporting queries."""
from typing import List

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models import Book, Order, OrderItem, OrderStatus
from app.schemas import TopBook


def top_books(db: Session, limit: int = 5) -> List[TopBook]:
    """Best-selling books.

    Rules: copies_sold sums quantities over ``paid`` orders only; books with no sales are
    excluded; sorted by copies_sold desc, then title asc; at most ``limit`` rows.
    """
    stmt = (
        select(
            OrderItem.book_id,
            Book.title,
            func.sum(OrderItem.quantity).label("copies_sold"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Book, Book.id == OrderItem.book_id)
        .where(Order.status == OrderStatus.PAID.value)
        .group_by(OrderItem.book_id, Book.title)
        .order_by(
            desc("copies_sold"),
            Book.title.asc(),
        )
        .limit(limit)
    )

    rows = db.execute(stmt).all()

    return [
        TopBook(
            book_id=book_id,
            title=title,
            copies_sold=int(copies_sold),
        )
        for book_id, title, copies_sold in rows
    ]
