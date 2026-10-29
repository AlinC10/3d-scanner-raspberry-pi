from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app_paths import results_directory
from schemas.library import LibraryObject, LibraryPage, ModelRequest
from storage import results

router = APIRouter(
    prefix="/library",
    tags=["library"],
)

# Most objects one page may ask for
MAX_PAGE_SIZE = 100


@router.get("/objects", response_model=LibraryPage)
def list_objects(
    offset: Annotated[int, Query(ge=0, description="Index of the first object to return.")] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="How many objects to return.")] = 20,
):
    """
    The finished 3D objects, newest first: their id and `object.json`, and which models exist. No model file is
    sent here; ask for one with `GET /library/objects/{id}/model`. Continue with `offset=next_offset` until it is null.
    """
    page = results.list_results(results_directory(), offset=offset, limit=limit)
    return LibraryPage(
        items=[LibraryObject(**entry.as_dict()) for entry in page.items],
        total=page.total, offset=page.offset, limit=page.limit, next_offset=page.next_offset,
    )


@router.get("/objects/{object_id}", response_model=LibraryObject)
def get_object(object_id: str):
    """One finished object (404 when it does not exist)."""
    try:
        entry = results.get_result(results_directory(), object_id)
    except results.ResultNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return LibraryObject(**entry.as_dict())


@router.get("/objects/{object_id}/model")
def get_model(object_id: str, query: Annotated[ModelRequest, Query()]):
    """
    One model of an object, as a file: `format` `.glb` or `.stl` (`glb`, `stl`, `gltf` are accepted too), `lod`
    `low` or `high`. 404 when the object or that model does not exist, 422 for another format.
    """
    try:
        path = results.model_path(results_directory(), object_id, query.format, query.lod)
    except results.ResultNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return FileResponse(
        path,
        media_type=results.MEDIA_TYPES[query.format],
        filename=f"{object_id}_{query.lod}{query.format}",
    )
