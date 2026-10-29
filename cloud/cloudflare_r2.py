import logging
import os
import shutil
import time
import uuid
import zipfile

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

load_dotenv()

from botocore.config import Config


class R2Error(Exception):
    """Raised for R2 problems the caller must react to (failed deletes, failed listing, failed preflight)."""


def _build_r2_config() -> Config:
    """
    Build the botocore Config used for the R2 client.

    boto3 >= 1.36 sends CRC checksums by default, which R2 can reject (CLOUD-16); the "when_required" options
    turn that off. They do not exist on older botocore, which raises TypeError, so fall back to the options it knows.
    """
    base = dict(
        connect_timeout=10,  # Fail fast if unable to open socket
        read_timeout=60,  # Fail if transfer stalls (a full-resolution JPEG can take a while on a slow uplink)
        retries={'max_attempts': 1},  # Disable internal blind retries; our worker controls backoff
    )
    attempts = (
        dict(base, region_name="auto",
             request_checksum_calculation="when_required",
             response_checksum_validation="when_required"),
        dict(base, region_name="auto"),
        base,
    )
    for options in attempts[:-1]:
        try:
            return Config(**options)
        except TypeError:
            continue
    return Config(**attempts[-1])


R2_CONFIG = _build_r2_config()

# Longest time one upload attempt may take in total (s). read_timeout only catches a connection that stalls completely;
# a transfer that keeps trickling a few bytes at a time is cut here and retried by the scanner's upload worker.
UPLOAD_ATTEMPT_TIMEOUT_S = float(os.environ.get("SCAN_UPLOAD_ATTEMPT_TIMEOUT_S", "300"))


class UploadAttemptTimeout(Exception):
    """An upload attempt ran longer than its time limit (raised from the transfer's progress callback)."""

s3 = boto3.client(
    service_name="s3",
    endpoint_url=os.environ.get("R2_ENDPOINT_URL"),
    aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"),
    config=R2_CONFIG
)

# R2 bucket that will only be used for hosting images that will be used in the Meshroom pipeline
# and after that will be deleted
R2_PIPELINE_IMAGES_BUCKET = "photogrammetry-pipeline"

from pathlib import Path

# Base directory is the project root (parent of 'cloud')
BASE_DIR = str(Path(__file__).resolve().parent.parent)

INPUT_IMAGES = os.path.join(BASE_DIR, "input_images")
# Rig mode: images split into input_images/rig/0/ (Camera 1) and input_images/rig/1/ (Camera 2)
RIG_IMAGES = os.path.join(INPUT_IMAGES, "rig")
# Two-sides mode: two rig directories for scanning both sides of an object
TWO_SIDES_RIG1 = os.path.join(INPUT_IMAGES, "rig1")
TWO_SIDES_RIG2 = os.path.join(INPUT_IMAGES, "rig2")

OUTPUT_DIR = os.path.join(BASE_DIR, "output")


def file_exists_and_is_new(object_name: str, bucket: str = R2_PIPELINE_IMAGES_BUCKET,
                           after_timestamp: float = 0.0) -> bool:
    """
    Check if an object exists in R2 and was modified after a specific UNIX timestamp.
    :param object_name: S3 object name
    :type object_name: str
    :param bucket: Bucket to check
    :type bucket: str
    :param after_timestamp: UNIX timestamp to check against
    :type after_timestamp: float
    :return: True if file exists and is newer than after_timestamp
    :rtype: bool
    """
    try:
        response = s3.head_object(Bucket=bucket, Key=object_name)
        # LastModified is a datetime object, convert to UNIX timestamp
        last_modified = response['LastModified'].timestamp()
        return last_modified >= after_timestamp
    except ClientError as e:
        if e.response['Error']['Code'] == '404':
            return False
        logging.error("[R2] head_object error for %s: %s", object_name, e)
        return False
    except BotoCoreError as e:
        logging.error("[R2] head_object error for %s: %s", object_name, e)
        return False


def delete_file(object_name: str, bucket: str = R2_PIPELINE_IMAGES_BUCKET) -> bool:
    """
    Delete a file from an S3 bucket.
    :param object_name: S3 object name
    :type object_name: str
    :param bucket: Bucket to delete from
    :type bucket: str
    :return: True if file was deleted, else False
    :rtype: bool
    """
    # Delete the file
    try:
        s3.delete_object(Bucket=bucket, Key=object_name)
    except (ClientError, BotoCoreError) as e:
        logging.error(e)
        return False
    return True


# delete_objects accepts at most 1000 keys per request
_DELETE_CHUNK = 1000


def list_keys(prefix: str = "", bucket: str = R2_PIPELINE_IMAGES_BUCKET) -> set[str]:
    """
    List every object key in the bucket (paginated, so more than 1000 objects are handled).
    :param prefix: Only keys starting with this prefix are returned ("" = all keys)
    :type prefix: str
    :param bucket: Bucket to list
    :type bucket: str
    :return: Set of object keys
    :rtype: set[str]
    :raises R2Error: if the listing fails
    """
    keys: set[str] = set()
    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                keys.add(obj['Key'])
    except (ClientError, BotoCoreError) as e:
        raise R2Error(f"Could not list bucket '{bucket}': {e}") from e
    return keys


def verify_keys(expected, bucket: str = R2_PIPELINE_IMAGES_BUCKET) -> list[str]:
    """
    Compare the expected keys with what is really in the bucket (one paginated listing).
    :param expected: Iterable of object keys that must be present
    :param bucket: Bucket to check
    :type bucket: str
    :return: Sorted list of expected keys that are missing from the bucket (empty = everything is there)
    :rtype: list[str]
    :raises R2Error: if the listing fails
    """
    present = list_keys(bucket=bucket)
    return sorted(set(expected) - present)


def delete_all_files_from_bucket(bucket: str = R2_PIPELINE_IMAGES_BUCKET) -> int:
    """
    Delete all files from R2 bucket (paginated listing, delete_objects in chunks of 1000).
    :param bucket: Bucket to delete from
    :type bucket: str
    :return: Number of objects deleted
    :rtype: int
    :raises R2Error: if listing or deleting fails, or if R2 reports per-key errors (the message lists the keys)
    """
    keys = sorted(list_keys(bucket=bucket))
    if not keys:
        logging.info("[R2] No files found in bucket '%s'.", bucket)
        return 0

    deleted = 0
    failed: list[str] = []
    for start in range(0, len(keys), _DELETE_CHUNK):
        chunk = keys[start:start + _DELETE_CHUNK]
        try:
            response = s3.delete_objects(
                Bucket=bucket,
                Delete={'Objects': [{'Key': key} for key in chunk], 'Quiet': True}
            )
        except (ClientError, BotoCoreError) as e:
            raise R2Error(f"Could not delete objects from bucket '{bucket}': {e}") from e
        errors = response.get('Errors') or []
        failed.extend(err.get('Key', '?') for err in errors)
        deleted += len(chunk) - len(errors)

    if failed:
        raise R2Error(
            f"Failed to delete {len(failed)} object(s) from bucket '{bucket}': " + ", ".join(sorted(failed))
        )
    logging.info("[R2] Cleaned up %d old files in bucket '%s'.", deleted, bucket)
    return deleted


def preflight(bucket: str = R2_PIPELINE_IMAGES_BUCKET) -> None:
    """
    Verify that uploads to R2 will work before a scan starts.

    Checks the R2_* environment variables, then does a small write + head + delete round trip on a throw-away key.
    This exercises the credentials, the bucket permissions and the checksum behaviour (CLOUD-16).
    :param bucket: Bucket to probe
    :type bucket: str
    :return: None
    :raises R2Error: with a human readable message when anything is wrong
    """
    missing = [
        name for name in ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise R2Error("Cloudflare R2 is not configured, missing environment variable(s): " + ", ".join(missing))

    key = f"_preflight/{uuid.uuid4().hex}.txt"
    written = False
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=b"preflight")
        written = True
        s3.head_object(Bucket=bucket, Key=key)
    except (ClientError, BotoCoreError, OSError) as e:
        raise R2Error(
            f"R2 write test failed on bucket '{bucket}' (check credentials, bucket name and permissions): {e}"
        ) from e
    finally:
        if written:
            try:
                s3.delete_object(Bucket=bucket, Key=key)
            except (ClientError, BotoCoreError, OSError) as e:
                raise R2Error(
                    f"R2 delete test failed on bucket '{bucket}' (probe object '{key}' may be left behind): {e}"
                ) from e


def upload_file(file_name: str, bucket: str = R2_PIPELINE_IMAGES_BUCKET, object_name: str | None = None,
                timeout_s: float | None = None) -> bool:
    """
    Upload a file to an S3 bucket. Never raises: every failure is logged and reported as False.
    :param file_name: File to upload
    :type file_name: str
    :param bucket: Bucket to upload to
    :type bucket: str
    :param object_name: S3 object name. If not specified then file_name is used
    :type object_name: str | None
    :param timeout_s: Total time limit of this attempt in seconds (default UPLOAD_ATTEMPT_TIMEOUT_S; 0 or less
        disables it). Checked whenever bytes are sent; a connection that sends nothing ends at the read timeout.
    :type timeout_s: float | None
    :return: True if file was uploaded, else False
    :rtype: bool
    """

    # If S3 object_name was not specified, use file_name
    if object_name is None:
        object_name = os.path.basename(file_name)

    # Do not touch the network for a file that is missing or empty (a 0-byte photo is a failed capture)
    try:
        size = os.path.getsize(file_name) if os.path.isfile(file_name) else None
    except OSError as e:
        logging.error("[R2] Cannot stat %s: %s", file_name, e)
        return False
    if size is None:
        logging.error("[R2] Not uploading %s: file does not exist", file_name)
        return False
    if size == 0:
        logging.error("[R2] Not uploading %s: file is empty (0 bytes)", file_name)
        return False

    limit = UPLOAD_ATTEMPT_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + limit

    def _check_deadline(_bytes_sent: int) -> None:
        if time.monotonic() > deadline:
            raise UploadAttemptTimeout(f"upload attempt longer than {limit:.0f} s")

    # Upload the file
    try:
        if limit > 0:
            s3.upload_file(file_name, bucket, object_name, Callback=_check_deadline)
        else:
            s3.upload_file(file_name, bucket, object_name)
    except (ClientError, BotoCoreError, S3UploadFailedError, OSError, UploadAttemptTimeout) as e:
        logging.error("[R2] Upload of %s as %s failed: %s", file_name, object_name, e)
        return False
    if limit > 0 and time.monotonic() > deadline:
        logging.warning("[R2] Upload of %s took longer than %.0f s but completed.", file_name, limit)
    return True


def create_output_zip(folder_path: str, zip_path: str) -> str:
    """
    Builds a structured ZIP from the pipeline output directory.

    ZIP layout:
        obj/high/       <- all files from Texturing_1/
        obj/low/        <- all files from Texturing_2/
        glb/high_model.glb
        glb/low_model.glb
        stl/high_model.stl
        stl/low_model.stl
        3mf/high_model.3mf
        3mf/low_model.3mf
    :param folder_path: Absolute or relative path to the output directory.
    :type folder_path: str
    :param zip_path: Destination path for the .zip file.
    :type zip_path: str
    :return: zip_path on success, raises on error.
    :rtype: str
    """
    texturing1_dir = os.path.join(folder_path, "Texturing_1")
    texturing2_dir = os.path.join(folder_path, "Texturing_2")
    glb_file = os.path.join(folder_path, "low_model.glb")
    high_glb_file = os.path.join(folder_path, "high_model.glb")
    high_stl_file = os.path.join(folder_path, "high_model.stl")
    low_stl_file = os.path.join(folder_path, "low_model.stl")

    os.makedirs(os.path.dirname(os.path.abspath(zip_path)), exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        # --- obj/high/ : everything inside Texturing_1/ ---
        if os.path.isdir(texturing1_dir):
            for fname in os.listdir(texturing1_dir):
                fpath = os.path.join(texturing1_dir, fname)
                if os.path.isfile(fpath):
                    zipf.write(fpath, os.path.join("obj", "high", fname))
        else:
            logging.warning("[ZIP] Texturing_1 directory not found: %s", texturing1_dir)

        # --- obj/low/ : everything inside Texturing_2/ ---
        if os.path.isdir(texturing2_dir):
            for fname in os.listdir(texturing2_dir):
                fpath = os.path.join(texturing2_dir, fname)
                if os.path.isfile(fpath):
                    zipf.write(fpath, os.path.join("obj", "low", fname))
        else:
            logging.warning("[ZIP] Texturing_2 directory not found: %s", texturing2_dir)

        # --- glb/low_model.glb ---
        if os.path.isfile(glb_file):
            zipf.write(glb_file, os.path.join("glb", "low_model.glb"))
        else:
            logging.warning("[ZIP] GLB file not found: %s", glb_file)

        # --- glb/high_model.glb ---
        if os.path.isfile(high_glb_file):
            zipf.write(high_glb_file, os.path.join("glb", "high_model.glb"))

        # --- stl/high_model.stl ---
        if os.path.isfile(high_stl_file):
            zipf.write(high_stl_file, os.path.join("stl", "high_model.stl"))
        else:
            logging.warning("[ZIP] High STL file not found: %s", high_stl_file)

        # --- stl/low_model.stl ---
        if os.path.isfile(low_stl_file):
            zipf.write(low_stl_file, os.path.join("stl", "low_model.stl"))
        else:
            logging.warning("[ZIP] Low STL file not found: %s", low_stl_file)

        # --- 3mf/high_model.3mf ---
        high_3mf_file = os.path.join(folder_path, "high_model.3mf")
        if os.path.isfile(high_3mf_file):
            zipf.write(high_3mf_file, os.path.join("3mf", "high_model.3mf"))
        else:
            logging.warning("[ZIP] High 3MF file not found: %s", high_3mf_file)

        # --- 3mf/low_model.3mf ---
        low_3mf_file = os.path.join(folder_path, "low_model.3mf")
        if os.path.isfile(low_3mf_file):
            zipf.write(low_3mf_file, os.path.join("3mf", "low_model.3mf"))
        else:
            logging.warning("[ZIP] Low 3MF file not found: %s", low_3mf_file)

        # --- stats.json ---
        stats_file = os.path.join(folder_path, "stats.json")
        if os.path.isfile(stats_file):
            zipf.write(stats_file, "stats.json")

    logging.info("[ZIP] Successfully created %s", zip_path)
    return zip_path


def upload_generated_obj(folder_path: str, object_name: str = "output.zip",
                         bucket: str = R2_PIPELINE_IMAGES_BUCKET) -> bool:
    """
    Zips the pipeline output directory and uploads it to R2.

    ZIP structure:
        obj/high/       <- Texturing_1 contents (OBJ + PNG textures)
        obj/low/        <- Texturing_2 contents (OBJ + JPG textures)
        glb/high_model.glb
        glb/low_model.glb
        stl/high_model.stl
        stl/low_model.stl
        3mf/high_model.3mf
        3mf/low_model.3mf
    :param folder_path: Relative or absolute path to the output directory.
    :type folder_path: str
    :param object_name: Key used when storing the file in R2.
    :type object_name: str
    :param bucket: Bucket to download from
    :type bucket: str
    :return: True if upload succeeded, False otherwise.
    :rtype: bool
    """
    zip_path = os.path.join(folder_path, "output.zip")

    print(f"  [R2] Building zip archive -> {zip_path}")
    try:
        create_output_zip(folder_path, zip_path)
    except Exception as e:
        logging.error("[R2] Failed to create zip: %s", e)
        return False

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"  [R2] Zip ready ({size_mb:.1f} MB). Uploading as '{object_name}'...")

    success = upload_file(zip_path, bucket, object_name)
    if success:
        print(f"  [R2] Upload complete: {object_name}")
    else:
        print(f"  [R2] Upload FAILED for: {object_name}")

    return success


def download_file(object_name: str, file_name: str | None = None, bucket: str = R2_PIPELINE_IMAGES_BUCKET) -> bool:
    """
    Download a file from an S3 bucket
    :param object_name: S3 object name
    :type object_name: str
    :param file_name: File to download. If not specified then object_name is used
    :type file_name: str | None
    :param bucket: Bucket to download from
    :type bucket: str
    :return: True if file was downloaded, else False
    :rtype: bool
    """

    # If file_name was not specified, use object_name
    if file_name is None:
        file_name = object_name

    # Download the file
    try:
        # s3.download_file('amzn-s3-demo-bucket', 'OBJECT_NAME', 'FILE_NAME')
        s3.download_file(bucket, object_name, file_name)
    except ClientError as e:
        logging.error(e)
        return False
    return True


def download_every_img_from_bucket(local_dir: str = INPUT_IMAGES,
                                   bucket: str = R2_PIPELINE_IMAGES_BUCKET,
                                   rig_mode: bool = False,
                                   two_sides_mode: bool = False):
    """
    Download all images from the R2 bucket to a local directory.

    Single mode (rig_mode=False, two_sides_mode=False):
        Downloads all objects flat into `local_dir/`.
        R2 keys: IMG_0001.jpg  →  local_dir/IMG_0001.jpg

    Rig mode (rig_mode=True):
        Downloads all objects preserving subfolder structure into RIG_IMAGES.
        R2 keys: rig/0/0001.jpg  →  input_images/rig/0/0001.jpg
                 rig/1/0001.jpg  →  input_images/rig/1/0001.jpg

    Two-sides mode (two_sides_mode=True):
        Downloads objects preserving rig1/rig2 subfolder structure.
        R2 keys: rig1/0/0001.jpg  →  input_images/rig1/0/0001.jpg
                 rig1/1/0001.jpg  →  input_images/rig1/1/0001.jpg
                 rig2/0/0001.jpg  →  input_images/rig2/0/0001.jpg
                 rig2/1/0001.jpg  →  input_images/rig2/1/0001.jpg
    :param local_dir: Local directory to download images into (used in single mode).
    :type local_dir: str
    :param bucket: R2 bucket name.
    :type bucket: str
    :param rig_mode: If True, download rig-structured images.
    :type rig_mode: bool
    :param two_sides_mode: If True, download two-sides structured images into rig1/rig2.
    :type two_sides_mode: bool
    :return: None
    :rtype: None
    """
    if two_sides_mode:
        os.makedirs(TWO_SIDES_RIG1, exist_ok=True)
        os.makedirs(TWO_SIDES_RIG2, exist_ok=True)

        print("Downloading images from R2 (two-sides mode)...")
        response = s3.list_objects_v2(Bucket=bucket)
        if 'Contents' in response:
            for obj in response['Contents']:
                file_key = obj['Key']
                # Keys are: rig1/0/0001.jpg, rig2/1/0001.jpg, etc.
                if file_key.startswith("rig1/"):
                    sub_path = file_key[len("rig1/"):]  # e.g. "0/0001.jpg"
                    local_path = os.path.join(TWO_SIDES_RIG1, *sub_path.split("/"))
                elif file_key.startswith("rig2/"):
                    sub_path = file_key[len("rig2/"):]
                    local_path = os.path.join(TWO_SIDES_RIG2, *sub_path.split("/"))
                else:
                    continue

                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                print(f"Downloading {file_key} -> {local_path}...")
                download_file(file_key, local_path, bucket)

        print("Downloaded every image (two-sides)")
        return

    if rig_mode:
        target_dir = RIG_IMAGES
        os.makedirs(target_dir, exist_ok=True)

        print("Downloading images from R2 (rig mode)...")
        response = s3.list_objects_v2(Bucket=bucket)
        if 'Contents' in response:
            for obj in response['Contents']:
                file_key = obj['Key']
                if not file_key.startswith("rig/"):
                    continue
                # file_key = "rig/0/0001.jpg". Strip "rig/" to get "0/0001.jpg"
                sub_path = file_key[len("rig/"):]
                local_path = os.path.join(target_dir, *sub_path.split("/"))
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                print(f"Downloading {file_key} -> {local_path}...")
                download_file(file_key, local_path, bucket)

        print("Downloaded every image (rig)")
        return

    # Single mode
    os.makedirs(local_dir, exist_ok=True)
    print("Downloading images from R2...")
    response = s3.list_objects_v2(Bucket=bucket)
    if 'Contents' in response:
        for obj in response['Contents']:
            file_key = obj['Key']
            local_path = os.path.join(local_dir, os.path.basename(file_key))
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            print(f"Downloading {file_key} -> {local_path}...")
            download_file(file_key, local_path, bucket)

    print("Downloaded every image")


def download_generated_obj(object_name: str = "output.zip", dest_dir: str | None = None,
                           bucket: str = R2_PIPELINE_IMAGES_BUCKET) -> bool:
    """
    Downloads the output ZIP from R2 and extracts it into dest_dir.

    Resulting layout after extraction:
        dest_dir/obj/high/       <- OBJ + PNG textures
        dest_dir/obj/low/        <- OBJ + JPG textures
        dest_dir/glb/high_model.glb
        dest_dir/glb/low_model.glb
        dest_dir/stl/high_model.stl
        dest_dir/stl/low_model.stl
        dest_dir/3mf/high_model.3mf
        dest_dir/3mf/low_model.3mf
    :param object_name: R2 key of the zip file (e.g. "output.zip").
    :type object_name: str
    :param dest_dir: Local directory where the zip is extracted. Created automatically if it does not exist.
    :type dest_dir: str | None
    :param bucket: Bucket to download from
    :type bucket: str
    :return: True if download + extraction succeeded, False otherwise.
    :rtype: bool
    """
    if not dest_dir:
        dest_dir = OUTPUT_DIR

    # Safely clean out old artifacts before extracting new ones
    if os.path.exists(dest_dir):
        # Safety guard: ensure we don't accidentally wipe root/current working dir
        if os.path.abspath(dest_dir) != os.path.abspath(os.getcwd()):
            shutil.rmtree(dest_dir, ignore_errors=True)

    os.makedirs(dest_dir, exist_ok=True)

    zip_path = os.path.join(dest_dir, "output.zip")

    print(f"  [R2] Downloading '{object_name}' -> {zip_path}")
    if not download_file(object_name, zip_path, bucket):
        print(f"  [R2] Download FAILED for: {object_name}")
        return False

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"  [R2] Downloaded ({size_mb:.1f} MB). Extracting to {dest_dir}...")

    try:
        with zipfile.ZipFile(zip_path, "r") as zipf:
            zipf.extractall(dest_dir)
    except zipfile.BadZipFile as e:
        logging.error("[R2] Extraction failed — bad zip: %s", e)
        return False
    finally:
        os.remove(zip_path)

    print(f"  [R2] Extraction complete: {dest_dir}")
    return True


def send_images(input_images_path: str = INPUT_IMAGES):
    """Upload flat single-camera images to R2.

    R2 keys will be the bare filenames: IMG_0001.jpg
    Use for mode='single'.
    """
    print(f"Uploading images to R2 (single mode)...")
    for img in os.listdir(input_images_path):
        img_path = os.path.join(input_images_path, img)
        if not os.path.isfile(img_path):
            continue
        print(f"  Uploading {img} to R2...")
        upload_file(img_path)

    print("All images uploaded to R2")


def send_rig_images(rig_images_path: str = RIG_IMAGES):
    """Upload rig-structured images to R2 preserving the subfolder layout.

    Walks 0/ and 1/ (or any numeric subfolders) and uploads each
    image with an R2 key that mirrors the local structure:
        rig_images_path/0/0001.jpg  →  R2 key: rig/0/0001.jpg
        rig_images_path/1/0001.jpg  →  R2 key: rig/1/0001.jpg

    Use for mode='rig'.
    """
    print(f"Uploading rig images to R2 (rig mode)...")
    for cam_folder in sorted(os.listdir(rig_images_path)):
        cam_path = os.path.join(rig_images_path, cam_folder)
        if not os.path.isdir(cam_path):
            continue
        for img in sorted(os.listdir(cam_path)):
            img_path = os.path.join(cam_path, img)
            if not os.path.isfile(img_path):
                continue
            # R2 key mirrors the subfolder structure: rig/<cam_folder>/<filename>
            r2_key = f"rig/{cam_folder}/{img}"
            print(f"  Uploading {r2_key}...")
            upload_file(img_path, object_name=r2_key)

    print("All rig images uploaded to R2")


def send_two_sides_images(rig1_path: str = TWO_SIDES_RIG1,
                          rig2_path: str = TWO_SIDES_RIG2):
    """Upload two-sides rig-structured images to R2.

    Walks rig1/ and rig2/ (each containing 0/ and 1/ camera subfolders)
    and uploads each image with an R2 key that mirrors the local structure:
        rig1_path/0/0001.jpg  →  R2 key: rig1/0/0001.jpg
        rig1_path/1/0001.jpg  →  R2 key: rig1/1/0001.jpg
        rig2_path/0/0001.jpg  →  R2 key: rig2/0/0001.jpg
        rig2_path/1/0001.jpg  →  R2 key: rig2/1/0001.jpg

    Use for mode='two-sides'.
    """
    print("Uploading images to R2 (two-sides mode)...")
    for rig_name, rig_path in [("rig1", rig1_path), ("rig2", rig2_path)]:
        if not os.path.isdir(rig_path):
            print(f"  WARNING: {rig_path} not found, skipping.")
            continue
        for cam_folder in sorted(os.listdir(rig_path)):
            cam_path = os.path.join(rig_path, cam_folder)
            if not os.path.isdir(cam_path):
                continue
            for img in sorted(os.listdir(cam_path)):
                img_path = os.path.join(cam_path, img)
                if not os.path.isfile(img_path):
                    continue
                # R2 key: rig1/0/0001.jpg
                r2_key = f"{rig_name}/{cam_folder}/{img}"
                print(f"  Uploading {r2_key}...")
                upload_file(img_path, object_name=r2_key)

    print("All two-sides images uploaded to R2")


def download_result():
    print("Download resulted model")
    download_generated_obj("output.zip", "./output")
    print("Model downloaded!")


def delete_all_files():
    print("Cleaning up R2 bucket...")
    delete_all_files_from_bucket()
    print("R2 bucket cleaned up!")
