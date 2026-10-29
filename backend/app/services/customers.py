from sqlalchemy.orm import Session

from backend.app.db.models import CustomerRecord


def customer_exists(db: Session, customer_id: str) -> bool:
    return db.get(CustomerRecord, customer_id) is not None
