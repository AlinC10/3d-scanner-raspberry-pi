from subprocess import CompletedProcess

class SystemConnectionError(Exception):
    """Base exception for system network & hardware errors."""
    pass

def check_and_raise_error(res: CompletedProcess, msg: str, err_type: type[Exception]) -> None:
    """
    Checks if a subprocess failed and raises the given exception class.

    :param res: The CompletedProcess from subprocess.run
    :type res: CompletedProcess
    :param msg: Custom error message prefix
    :type msg: str
    :param err_type: The Exception class to instantiate and raise
    :type err_type: type[Exception]
    """

    if res.returncode != 0:
        err = res.stderr.strip() or res.stdout.strip()
        raise err_type(msg + ":" + err)