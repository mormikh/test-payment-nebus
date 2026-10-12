from fastapi import FastAPI
from src.api.routes import payments

app = FastAPI(
    title="Payment Processing Service",
    description="Асинхронный сервис процессинга платежей",
    version="1.0.0",
)

app.include_router(payments.router, prefix="/api/v1", tags=["Payments"])

@app.get("/health")
async def health_check():
    return {"status": "healthy"}
