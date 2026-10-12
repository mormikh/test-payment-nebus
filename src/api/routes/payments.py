import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Header, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from faststream.rabbit import RabbitBroker

from src.api.deps import get_db_and_auth
from src.database import AsyncSession, async_session_maker
from src.models.payment import Payment
from src.models.outbox import OutboxEvent
from src.config import settings

router = APIRouter()

class PaymentCreateRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Сумма платежа")
    currency: str = Field(..., pattern="^(RUB|USD|EUR)$")
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)
    webhook_url: str = Field(..., min_length=10)

@router.post("/payments", status_code=status.HTTP_202_ACCEPTED)
async def create_payment(
    request: PaymentCreateRequest,
    background_tasks: BackgroundTasks,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    deps: dict = Depends(get_db_and_auth)
):
    db: AsyncSession = deps["db"]

    stmt = select(Payment).where(Payment.idempotency_key == idempotency_key)
    result = await db.execute(stmt)
    existing_payment = result.scalar_one_or_none()

    if existing_payment:
        return {
            "payment_id": str(existing_payment.id),
            "status": existing_payment.status,
            "created_at": existing_payment.created_at.isoformat(),
            "message": "Idempotent request, returning existing payment"
        }

    payment_id = uuid.uuid4()
    
    new_payment = Payment(
        id=payment_id,
        amount=request.amount,
        currency=request.currency,
        description=request.description,
        metadata_=request.metadata,
        webhook_url=request.webhook_url,
        idempotency_key=idempotency_key,
        status="pending"
    )
    db.add(new_payment)

    outbox_event = OutboxEvent(
        event_type="payment.created",
        aggregate_id=str(payment_id),
        payload={
            "payment_id": str(payment_id),
            "amount": str(request.amount),
            "currency": request.currency,
            "webhook_url": request.webhook_url
        },
        status="pending"
    )
    db.add(outbox_event)
    
    await db.flush()
    
    background_tasks.add_task(publish_outbox_event, str(payment_id))

    return {
        "payment_id": str(payment_id),
        "status": "pending",
        "created_at": new_payment.created_at.isoformat()
    }

async def publish_outbox_event(payment_id: str):
    broker = RabbitBroker(settings.rabbitmq_url)
    await broker.connect()
    
    try:
        async with async_session_maker() as db:
            stmt = select(OutboxEvent).where(
                OutboxEvent.aggregate_id == payment_id, 
                OutboxEvent.status == "pending"
            )
            result = await db.execute(stmt)
            event = result.scalar_one_or_none()
            
            if event:
                await broker.publish(event.payload, queue="payments.new")
                event.status = "succeeded"
                event.published_at = datetime.utcnow()
                await db.commit()
    finally:
        await broker.close()

@router.get("/payments/{payment_id}")
async def get_payment(
    payment_id: str,
    deps: dict = Depends(get_db_and_auth)
):
    db: AsyncSession = deps["db"]
    stmt = select(Payment).where(Payment.id == uuid.UUID(payment_id))
    result = await db.execute(stmt)
    payment = result.scalar_one_or_none()
    
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
        
    return {
        "payment_id": str(payment.id),
        "amount": float(payment.amount),
        "currency": payment.currency,
        "status": payment.status,
        "created_at": payment.created_at.isoformat(),
        "processed_at": payment.processed_at.isoformat() if payment.processed_at else None
    }
