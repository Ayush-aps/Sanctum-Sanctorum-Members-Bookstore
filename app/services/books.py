"""Book catalogue operations."""
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, or_,select
from sqlalchemy.orm import Session

from app.models import Book
from app.schemas import BookCreate, BookPage, BookSort, BookUpdate


def create_book(db: Session, data: BookCreate) -> Book:
    """Add a book to the catalogue.

    Rules: the (already normalized) ISBN must be unique -> 409 otherwise.
    """
    # TODO: reject a duplicate ISBN with 409

    existing_book = db.scalar(
        select(Book).where(Book.isbn == data.isbn)
    )

    if existing_book is not None:
        raise HTTPException(
            status_code=409,
            detail="A book with this ISBN already exists",
        )

    book = Book(**data.model_dump())
    db.add(book)
    db.commit()
    db.refresh(book)
    return book


def get_book(db: Session, book_id: int) -> Book:
    """Return a book by id, or raise 404."""
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    return book


def update_book(db: Session, book_id: int, data: BookUpdate) -> Book:
    """Apply a partial update. Only fields present in the request are changed; 404 if missing. ISBN is not patchable and is ignored if supplied. Unknown fields are ignored by the schema."""
    
    book = db.get(Book, book_id)

    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    update_data = data.model_dump(exclude_unset=True)

    # ISBN is intentionally not patchable.
    update_data.pop("isbn", None)

    for field, value in update_data.items():
        setattr(book, field, value)

    db.commit()
    db.refresh(book)

    return book

    # raise NotImplementedError("update_book")


def list_books(
    db: Session,
    q: Optional[str] = None,
    restricted: Optional[bool] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    sort: Optional[BookSort] = None,
    limit: int = 20,
    offset: int = 0,
) -> BookPage:
    """Search the catalogue.

    Rules:
    - ``q`` matches title OR author, case-insensitive substring.
    - ``restricted`` filters exactly; ``min_price``/``max_price`` are inclusive.
    - Sorted by ``sort`` (title / price, ``-`` for descending) with ties broken by id;
      default order is id ascending.
    - ``total`` counts all matches before ``limit``/``offset`` are applied.
    """
    query = select(Book)
    if q:
        query = query.where(
            or_(
                Book.title.icontains(q, autoescape=True),
                Book.author.icontains(q, autoescape=True),
                ))
    if restricted is not None:
        query = query.where(Book.restricted == restricted)

    # TODO: min_price / max_price filters
    # Inclusive minimum price.
    if min_price is not None:
        query = query.where(Book.price_cents >= min_price)

    # Inclusive maximum price.
    if max_price is not None:
        query = query.where(Book.price_cents <= max_price)

    # Count all matching books BEFORE limit/offset.
    total = db.scalar(
        select(func.count()).select_from(query.subquery())
    ) or 0

    # TODO: apply ``sort``

    if sort is None:
        ordered_query = query.order_by(Book.id.asc())
    else:
        sort_value = sort.value if hasattr(sort, "value") else str(sort)

        if sort_value == "title":
            ordered_query = query.order_by(
                Book.title.asc(),
                Book.id.asc(),
            )
        elif sort_value == "-title":
            ordered_query = query.order_by(
                Book.title.desc(),
                Book.id.asc(),
            )
        elif sort_value == "price":
            ordered_query = query.order_by(
                Book.price_cents.asc(),
                Book.id.asc(),
            )
        elif sort_value == "-price":
            ordered_query = query.order_by(
                Book.price_cents.desc(),
                Book.id.asc(),
            )
        else:
            # This should normally never be reached because BookSort
            # validation is handled by FastAPI/Pydantic.
            raise HTTPException(
                status_code=422,
                detail="Invalid sort value",
            )

    # Applied pagination only after filtering and counting.
    books = db.scalars(
        ordered_query
        .limit(limit)
        .offset(offset)
    ).all()

    #books = db.scalars(query.order_by(Book.id.asc()).limit(limit).offset(offset)).all()
    #total = len(books)

    return BookPage(items=books, total=total, limit=limit, offset=offset)
