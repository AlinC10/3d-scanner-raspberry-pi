import logging
import boto3
import os
from dotenv import load_dotenv
from botocore.exceptions import ClientError
import zipfile

load_dotenv()

from botocore.config import Config

R2_CONFIG = Config(
    connect_timeout=5,          # Fail fast if unable to open socket
    read_timeout=15,            # Fail if transfer stalls
    retries={'max_attempts': 1} # Disable internal blind retries; our worker controls backoff
)

s3 = boto3.client(
  service_name="s3",
  endpoint_url=os.environ.get("R2_ENDPOINT_URL"),
  aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
  aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"),
  config=R2_CONFIG
)

# R2 bucket that will only be used for hosting images that will be used in the Meshroom pipeline
# and after that will be deleted
R2_PIPELINE_IMAGES_BUCKET="photogrammetry-pipeline"

INPUT_IMAGES="./input_images"
# Rig mode: images split into ./input_images/rig/0/ (Camera 1) and ./input_images/rig/1/ (Camera 2)
RIG_IMAGES="./input_images/rig"
# Two-sides mode: two rig directories for scanning both sides of an object
TWO_SIDES_RIG1="./input_images/rig1"
TWO_SIDES_RIG2="./input_images/rig2"

OUTPUT_DIR="./output"


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
  except ClientError as e:
      logging.error(e)
      return False
  return True

def delete_all_files_from_bucket(bucket: str = R2_PIPELINE_IMAGES_BUCKET) -> None:
  """
  Delete all files from R2 bucket.
  :param bucket: Bucket to delete from
  :type bucket: str
  :return: None
  :rtype: None
  """
  response = s3.list_objects_v2(Bucket=bucket)

  if 'Contents' in response:
      # 2. Build a list of files to delete
      objects_to_delete = [
          {'Key': obj['Key']}
          for obj in response['Contents']
      ]

      # 3. Delete them all in one batch operation (Free operation!)
      if objects_to_delete:
          s3.delete_objects(
              Bucket=bucket,
              Delete={'Objects': objects_to_delete}
          )
          print(f"Cleaned up {len(objects_to_delete)} old files. Bucket is ready for the next scan!")

      return

  print("No files found in the bucket.")

def upload_file(file_name: str, bucket: str = R2_PIPELINE_IMAGES_BUCKET, object_name: str | None = None) -> bool:
  """
  Upload a file to an S3 bucket
  :param file_name: File to upload
  :type file_name: str
  :param bucket: Bucket to upload to
  :type bucket: str
  :param object_name: S3 object name. If not specified then file_name is used
  :type object_name: str | None
  :return: True if file was uploaded, else False
  :rtype: bool
  """

  # If S3 object_name was not specified, use file_name
  if object_name is None:
      object_name = os.path.basename(file_name)

  # Upload the file
  try:
      s3.upload_file(file_name, bucket, object_name)
  except ClientError as e:
      logging.error(e)
      return False
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
      dest_dir = os.getcwd()

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