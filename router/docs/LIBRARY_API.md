# Library API (`/library`)

The finished 3D objects made by Turntable Studio, read from the results folder (`results/` in the project, or the
`RESULTS_DIR` environment variable). One folder per object, named `YYYYMMDD-HHMMSS-<6 hex>`: that name is the object
`id`. Code: `../library.py` (router), `../../storage/results.py` (disk), `../../schemas/library.py` (schemas).

A folder is listed only when it is a finished result: `status.json` with `"state": "ok"` and a valid `object.json`.
Anything else (a reconstruction still being written, a broken folder) is skipped and logged; it never fails the list.
A missing results folder is an empty list.

| Method | Path | Answer |
|---|---|---|
| `GET` | `/library/objects?offset=0&limit=20` | One page of objects, newest first, without model files |
| `GET` | `/library/objects/{id}` | One object (404 when it does not exist or is not finished) |
| `GET` | `/library/objects/{id}/model?format=.glb&lod=low` | One model file |

## `GET /library/objects`

`offset` >= 0 (default 0), `limit` 1..100 (default 20); another value is a 422. Continue with `offset=next_offset`
until `next_offset` is `null`.

```json
{
  "items": [
    {
      "id": "20261027-202750-21d2de",
      "object": {"title": "Parkside", "dimensions_mm": {"length": 71.0, "width": 54.3, "height": 226.9}, "...": "..."},
      "models": {".glb": ["low", "high"], ".stl": ["low", "high"]}
    }
  ],
  "total": 1,
  "offset": 0,
  "limit": 20,
  "next_offset": null
}
```

`object` is the object's `object.json` as written by Turntable Studio. `models` lists only the files that exist.
The order is the folder name, descending: offsets stay valid as long as no new object is added while paging.

## `GET /library/objects/{id}/model`

| Query | Values | Default |
|---|---|---|
| `format` | `.glb` or `.stl`; also accepted: `glb`, `stl`, `gltf`, `.gltf` (any case; a glTF request gets the binary `.glb`) | `.glb` |
| `lod` | `low` (light, load it first) or `high` (full detail) | `low` |

The answer is the file (`model/gltf-binary` or `model/stl`) with `Content-Disposition` `<id>_<lod><format>`, `ETag`
and `Last-Modified`, and range requests are supported. 404: unknown object or that model is missing. 422: another
format or lod. The file name is built by the server from `format` and `lod`; the `id` must match the folder pattern,
so a path such as `../` never reaches the disk.

Example (browser, three.js):

```js
const page = await (await fetch("/library/objects?limit=20")).json();
const url = `/library/objects/${page.items[0].id}/model?format=glb&lod=low`;
new GLTFLoader().load(url, gltf => scene.add(gltf.scene));      // STLLoader for format=stl
```
