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

## `PATCH /library/objects/{id}` → `200`

Edits the object's `object.json`; only the fields sent (and not `null`) change. Body (all optional, unknown keys `422`):

```json
{"title": "Vase", "description": "Blue ceramic vase", "dimensions_mm": {"height": 230.0}, "volume_cm3": 400.0}
```

| Field | Rules |
|---|---|
| `title` | 1..120 characters |
| `description` | at most 2000 characters |
| `dimensions_mm` | any of `length`, `width`, `height` (mm, `> 0`); the sides not sent keep their value |
| `volume_cm3` | `> 0` |

Answer: the updated object (like `GET /library/objects/{id}`). `404` unknown object, `422` invalid value or a field that
cannot be edited (`created_at`, `job_id`, `faces_*`, ...). The file is written atomically (temporary file + rename).

## Object names

An object scan without `cloud.title` and a face capture without `title` are named after their folder (the job id, e.g.
`20261030-101500-a1b2c3`), the same name as the archive of a USB export. Rename them with `PATCH`.

## Scan photos

The photos an object was built from are copied into `results/<id>/photos/rig/0|1/` (with `manifest.json`) once the model
is saved. They are not served by the API and not part of the USB export.

## USB export

| Method | Path | Answer |
|---|---|---|
| `GET` | `/library/usb` | The USB sticks plugged in: `[{device, label, size_bytes, fstype, mountpoint, free_bytes}]` (empty list = none) |
| `POST` | `/library/objects/{id}/export/usb` | `202` + export status; body optional `{"device": "/dev/sda1"}` (default: the first stick) |
| `GET` | `/library/export/usb/status` | Progress of the export |

The export writes `<id>.zip` in the root of the stick: every model format (`obj/` with its textures, `glb/`, `stl/`,
`3mf/`) and `object.json`; logs, `output.zip`, `photos/` are left out. An archive of the same object already on the stick
is replaced (no duplicates). The stick is checked when the export is asked for: none plugged in = `503` ("No USB stick
is plugged in"). Then, in the background: mount (`udisksctl`), free space check, archive written as a hidden `.part`
file and renamed when complete, `sync` and unmount.

Status: `state` `idle` | `mounting` | `archiving` | `syncing` | `done` | `error`, `bytes_done` / `bytes_total`
(progress bar), `archive`, `error`, and **`safe_to_remove`**: false while the stick is in use; show "you can remove the
stick" only when it is true. Errors: `404` unknown object, `409` an export is already running, `503` no stick, `500`
`lsblk` / `udisksctl` failed. On the Pi the service needs the polkit rule in `kiosk/50-3d-scanner-udisks.rules`.

## Bluetooth export

| Method | Path | Answer |
|---|---|---|
| `POST` | `/library/objects/{id}/export/bluetooth` | `202` + status; body optional `{"mac_address": "..."}` (default: the first connected device) |
| `GET` | `/library/export/bluetooth/status` | `state`: `idle` \| `archiving` \| `sending` \| `done` \| `error` \| `cancelled` |
| `POST` | `/library/export/bluetooth/cancel` | Stops the transfer, the device stays connected: `{"cancelled": true}` |

Sends the same `<id>.zip` as the USB export to the device the Pi is connected to (`POST /system/bluetooth/connect`), by
OBEX push (`bt-obex`, package `bluez-tools`); the phone asks the user to accept the file (Android; iPhones do not accept
OBEX). No device connected = `503` ("No Bluetooth device is connected"), a transfer running = `409`, unknown object = `404`.
`POST /system/bluetooth/disconnect` with the receiving device cancels the transfer first (`state: "cancelled"`). There is
no byte progress (OBEX push does not report it): show a spinner while `sending`. A transfer longer than 10 min is stopped.
