from fastapi import APIRouter

from app.api.v1.endpoints import cv_analyses

api_router = APIRouter()

api_router.include_router(cv_analyses.router, tags=["CV analyses"])
