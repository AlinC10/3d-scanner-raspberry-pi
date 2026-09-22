from pathlib import Path
import shutil

def create_clean_dir(dir_path: str | Path) -> Path:
    """
    Safely recreate a directory: removes existing content/dir and creates a fresh one.

    :param dir_path: Relative or absolute path where the directory should be created clean
    :type dir_path: str | Path
    :return: Absolute path of the clean directory created
    :rtype: Path
    """
    path = Path(dir_path).resolve()

    # Safety check: NEVER delete root, home, or empty paths!
    if str(path) in ("/", str(Path.home())) or len(path.parts) <= 2:
        raise ValueError(f"Dangerous path deletion prevented: {path}")

    # If it exists, delete it completely
    if path.exists():
        shutil.rmtree(path)

    # Recreate the fresh directory (parents=True creates parent dirs if needed)
    path.mkdir(parents=True, exist_ok=True)
    return path