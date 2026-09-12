import logging
import boto3
import os
from dotenv import load_dotenv
from botocore.exceptions import ClientError
import zipfile

load_dotenv()

s3 = boto3.client(
  service_name="s3",
  endpoint_url=os.environ.get("R2_ENDPOINT_URL"),
  aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
  aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"),
)

# R2 bucket that will only be used for hosting images that will be used in the Meshroom pipeline
# and after that will be deleted
R2_PIPELINE_IMAGES_BUCKET="photogrammetry-pipeline"

INPUT_IMAGES="./input_images"

OUTPUT_DIR="./output"


def delete_file(object_name: str, bucket: str = R2_PIPELINE_IMAGES_BUCKET) -> bool:
  """Delete a file from an S3 bucket

  :param object_name: S3 object name
  :param bucket: Bucket to delete from
  :return: True if file was deleted, else False
  """
  # Delete the file
  try:
      s3.delete_object(Bucket=bucket, Key=object_name)
  except ClientError as e:
      logging.error(e)
      return False
  return True

def delete_all_files_from_bucket(bucket: str = R2_PIPELINE_IMAGES_BUCKET) -> None:
  """Delete all files from R2 bucket.

  :param bucket: Bucket to delete from
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
  """Upload a file to an S3 bucket

  :param file_name: File to upload
  :param bucket: Bucket to upload to
  :param object_name: S3 object name. If not specified then file_name is used
  :return: True if file was uploaded, else False
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

  :param folder_path: Absolute or relative path to the output directory.
  :param zip_path:    Destination path for the .zip file.
  :return: zip_path on success, raises on error.
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

  :param folder_path:  Relative or absolute path to the output directory.
  :param object_name:  Key used when storing the file in R2.
  :param bucket: Bucket to download from
  :return: True if upload succeeded, False otherwise.
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
  """Download a file from an S3 bucket

  :param object_name: S3 object name
  :param file_name: File to download. If not specified then object_name is used
  :param bucket: Bucket to download from
  :return: True if file was downloaded, else False
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

def download_every_img_from_bucket(local_dir: str = INPUT_IMAGES, bucket: str = R2_PIPELINE_IMAGES_BUCKET):
  response = s3.list_objects_v2(Bucket=bucket)
  os.makedirs(local_dir, exist_ok=True)

  print("Downloading images from R2...")
  if 'Contents' in response:
      for obj in response['Contents']:
          file_key = obj['Key']

          print(f"Downloading {file_key}...")

          download_file(file_key, os.path.join(local_dir, os.path.basename(file_key)), bucket)
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

  :param object_name: R2 key of the zip file (e.g. "output.zip").
  :param dest_dir:    Local directory where the zip is extracted.
                      Created automatically if it does not exist.
  :param bucket: Bucket to download from
  :return: True if download + extraction succeeded, False otherwise.
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