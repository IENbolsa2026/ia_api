from fastapi import APIRouter

from app.api.v1.endpoints import cv_analyses, matches, llm_usage

api_router = APIRouter()

api_router.include_router(cv_analyses.router, tags=["CV analyses"])
api_router.include_router(matches.router, tags=["Compatibilidad"])

api_router.include_router(llm_usage.router, tags=["Monitoreo IA"])
