from fastapi import FastAPI
from pydantic import BaseModel

from app.config import settings

app = FastAPI(title=settings.app_name)


class HealthResponse(BaseModel):
    status: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")
