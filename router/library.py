from fastapi import APIRouter

router = APIRouter(
    prefix="/library",
    tags=["library"],
)