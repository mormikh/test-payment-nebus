from fastapi import Depends, HTTPException, status, Header
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.config import settings

async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    if x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
        )
    return x_api_key

async def get_db_and_auth(
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    return {"db": db, "api_key": api_key}
