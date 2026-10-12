import asyncio
import random
import httpx
from datetime import datetime
from faststream import FastStream
from faststream.rabbit import RabbitBroker, RabbitQueue, RabbitExchange, ExchangeType
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from sqlalchemy import update

from src.config import settings
from src.database import async_session_maker
from src.models.payment import Payment
from src.models.outbox import OutboxEvent

DLQ_EXCHANGE_NAME = "payments.dlx"
DLQ_QUEUE_NAME = "payments.new.dlq"

dlq_exchange = RabbitExchange(name=DLQ_EXCHANGE_NAME, type=ExchangeType.DIRECT, durable=True)
dlq_queue = RabbitQueue(name=DLQ_QUEUE_NAME, durable=True)

main_queue = RabbitQueue(
    name="payments.new",
    durable=True,
    arguments={
        "x-dead-letter-exchange": DLQ_EXCHANGE_NAME,
        "x-dead-letter-routing-key": DLQ_QUEUE_NAME,
    }
)

broker = RabbitBroker(settings.rabbitmq_url)
app = FastStream(broker)

@broker.subscriber(queue=main_queue)
async def process_payment(message: dict):
    payment_id = message.get("payment_id")
    webhook_url = message.get("webhook_url")
    
    try:
        await _process_with_retry(payment_id, webhook_url)
    except Exception as e:
        print(f"⚠️ Payment {payment_id} failed after 3 retries. Sending to DLQ. Error: {e}")
        await broker.publish(
            message, 
            exchange=DLQ_EXCHANGE_NAME, 
            routing_key=DLQ_QUEUE_NAME
        )
        return


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def _process_with_retry(payment_id: str, webhook_url: str):
    await asyncio.sleep(random.uniform(2, 5))
    if random.random() < 0.1:
        raise Exception("Simulated payment gateway error (10% chance)")
    
    async with async_session_maker() as db:
        async with db.begin():
            await db.execute(
                update(Payment)
                .where(Payment.id == payment_id)
                .values(status="succeeded", processed_at=datetime.utcnow())
            )
            await db.execute(
                update(OutboxEvent)
                .where(OutboxEvent.aggregate_id == payment_id)
                .values(status="succeeded", published_at=datetime.utcnow())
            )

    await send_webhook_with_retry(payment_id, "succeeded", webhook_url)


@broker.subscriber(queue=dlq_queue, exchange=dlq_exchange)
async def process_dlq_payment(message: dict):
    payment_id = message.get("payment_id")
    webhook_url = message.get("webhook_url")
    
    async with async_session_maker() as db:
        async with db.begin():
            await db.execute(
                update(Payment)
                .where(Payment.id == payment_id)
                .values(status="failed", processed_at=datetime.utcnow())
            )
            
    try:
        async with httpx.AsyncClient() as client:
            await client.post(webhook_url, json={"payment_id": payment_id, "status": "failed"}, timeout=5.0)
    except Exception:
        print(f"Failed to send DLQ webhook for {payment_id}")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(httpx.HTTPError)
)
async def send_webhook_with_retry(payment_id: str, status: str, webhook_url: str):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            webhook_url,
            json={"payment_id": payment_id, "status": status},
            timeout=5.0
        )
        if response.status_code >= 500:
            response.raise_for_status()

if __name__ == "__main__":
    import asyncio
    asyncio.run(app.run())
