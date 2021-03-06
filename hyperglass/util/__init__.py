"""Utility functions."""

# Standard Library
import re
import math
import shutil
from queue import Queue
from typing import Dict, Iterable
from pathlib import Path
from threading import Thread

# Third Party
from loguru._logger import Logger as LoguruLogger

# Project
from hyperglass.log import log


def cpu_count(multiplier: int = 0):
    """Get server's CPU core count.

    Used for number of web server workers.

    Returns:
        {int} -- CPU Cores
    """
    import multiprocessing

    return multiprocessing.cpu_count() * multiplier


def clean_name(_name):
    """Remove unsupported characters from field names.

    Converts any "desirable" seperators to underscore, then removes all
    characters that are unsupported in Python class variable names.
    Also removes leading numbers underscores.

    Arguments:
        _name {str} -- Initial field name

    Returns:
        {str} -- Cleaned field name
    """
    _replaced = re.sub(r"[\-|\.|\@|\~|\:\/|\s]", "_", _name)
    _scrubbed = "".join(re.findall(r"([a-zA-Z]\w+|\_+)", _replaced))
    return _scrubbed.lower()


def check_path(path, mode="r", create=False):
    """Verify if a path exists and is accessible.

    Arguments:
        path {Path|str} -- Path object or string of path
        mode {str} -- File mode, r or w

    Raises:
        RuntimeError: Raised if file does not exist or is not accessible

    Returns:
        {Path|None} -- Path object if checks pass, None if not.
    """

    try:
        if not isinstance(path, Path):
            path = Path(path)

        if not path.exists():
            if create:
                if path.is_file():
                    path.parent.mkdir(parents=True)
                else:
                    path.mkdir(parents=True)
            else:
                raise FileNotFoundError(f"{str(path)} does not exist.")

        with path.open(mode):
            result = path

    except Exception:
        result = None

    return result


def check_python():
    """Verify Python Version.

    Raises:
        RuntimeError: Raised if running Python version is invalid.

    Returns:
        {str} -- Python version
    """
    import sys
    import platform
    from hyperglass.constants import MIN_PYTHON_VERSION

    pretty_version = ".".join(tuple(str(v) for v in MIN_PYTHON_VERSION))
    if sys.version_info < MIN_PYTHON_VERSION:
        raise RuntimeError(f"Python {pretty_version}+ is required.")
    return platform.python_version()


async def build_ui(app_path):
    """Execute `next build` & `next export` from UI directory.

    Raises:
        RuntimeError: Raised if exit code is not 0.
        RuntimeError: Raised when any other error occurs.
    """
    import os
    import asyncio

    try:
        timeout = os.environ["HYPERGLASS_UI_BUILD_TIMEOUT"]
        log.info("Found UI build timeout environment variable: {}", timeout)
        timeout = int(timeout)
    except KeyError:
        timeout = 90

    ui_dir = Path(__file__).parent.parent / "ui"
    build_dir = app_path / "static" / "ui"

    build_command = "node_modules/.bin/next build"
    export_command = "node_modules/.bin/next export -o {f}".format(f=build_dir)

    all_messages = []
    for command in (build_command, export_command):
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd=command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ui_dir,
            )

            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            messages = stdout.decode("utf-8").strip()
            errors = stderr.decode("utf-8").strip()

            if proc.returncode != 0:
                raise RuntimeError(f"\nMessages:\n{messages}\nErrors:\n{errors}")

            await proc.wait()
            all_messages.append(messages)

        except asyncio.TimeoutError:
            raise RuntimeError(f"{timeout} second timeout exceeded while building UI")

        except Exception as e:
            raise RuntimeError(str(e))

    return "\n".join(all_messages)


async def write_env(variables):
    """Write environment variables to temporary JSON file.

    Arguments:
        variables {dict} -- Environment variables to write.

    Raises:
        RuntimeError: Raised on any errors.
    """
    from aiofile import AIOFile
    import json

    env_file = Path("/tmp/hyperglass.env.json")  # noqa: S108
    env_vars = json.dumps(variables)

    try:
        async with AIOFile(env_file, "w+") as ef:
            await ef.write(env_vars)
            await ef.fsync()
    except Exception as e:
        raise RuntimeError(str(e))

    return f"Wrote {env_vars} to {str(env_file)}"


async def check_redis(db, config):
    """Ensure Redis is running before starting server.

    Arguments:
        db {int} -- Redis database ID
        config {dict} -- Redis configuration parameters

    Raises:
        RuntimeError: Raised if Redis is not running.

    Returns:
        {bool} -- True if redis is running.
    """
    import aredis

    redis_instance = aredis.StrictRedis(db=db, **config)
    redis_host = config["host"]
    redis_port = config["port"]
    try:
        await redis_instance.echo("hyperglass test")
    except Exception:
        raise RuntimeError(
            f"Redis isn't running at: {redis_host}:{redis_port}"
        ) from None
    return True


async def clear_redis_cache(db, config):
    """Clear the Redis cache.

    Arguments:
        db {int} -- Redis database ID
        config {dict} -- Redis configuration parameters

    Raises:
        RuntimeError: Raised if clearing the cache produces an error.

    Returns:
        {bool} -- True if cache was cleared.
    """
    import aredis

    try:
        redis_instance = aredis.StrictRedis(db=db, **config)
        await redis_instance.flushdb()
    except Exception as e:
        raise RuntimeError(f"Error clearing cache: {str(e)}") from None
    return True


async def move_files(src, dst, files):  # noqa: C901
    """Move iterable of files from source to destination.

    Arguments:
        src {Path} -- Current directory of files
        dst {Path} -- Target destination directory
        files {Iterable} -- Iterable of files
    """

    from typing import Iterable

    def error(*args, **kwargs):
        msg = ", ".join(args)
        kwargs = {k: str(v) for k, v in kwargs.items()}
        error_msg = msg.format(**kwargs)
        log.error(error_msg)
        return RuntimeError(error_msg)

    if not isinstance(src, Path):
        try:
            src = Path(src)
        except TypeError:
            raise error("{p} is not a valid path", p=src)

    if not isinstance(dst, Path):
        try:
            dst = Path(dst)
        except TypeError:
            raise error("{p} is not a valid path", p=dst)

    if not isinstance(files, Iterable):
        raise error(
            "{fa} must be an iterable (list, tuple, or generator). Received {f}",
            fa="Files argument",
            f=files,
        )

    for path in (src, dst):
        if not path.exists():
            raise error("{p} does not exist", p=path)

    migrated = ()

    for file in files:
        dst_file = dst / file.name

        if not file.exists():
            raise error("{f} does not exist", f=file)

        try:
            if not dst_file.exists():
                shutil.copyfile(file, dst_file)
                migrated += (str(dst_file),)
        except Exception as e:
            raise error("Failed to migrate {f}: {e}", f=dst_file, e=e)

    return migrated


def migrate_static_assets(app_path):
    """Synchronize the project assets with the installation assets."""

    from filecmp import dircmp

    asset_dir = Path(__file__).parent.parent / "images"
    target_dir = app_path / "static" / "images"

    target_exists = target_dir.exists()

    if not target_exists:
        shutil.copytree(asset_dir, target_dir)

    # Compare the contents of the project's asset directory (considered
    # the source of truth) with the installation directory. If they do
    # not match, delete the installation directory's asset directory and
    # re-copy it.
    compare_initial = dircmp(asset_dir, target_dir, ignore=[".DS_Store"])

    if not compare_initial.left_list == compare_initial.right_list:
        shutil.rmtree(target_dir)
        shutil.copytree(asset_dir, target_dir)

        # Re-compare the source and destination directory contents to
        # ensure they match.
        compare_post = dircmp(asset_dir, target_dir, ignore=[".DS_Store"])

        if not compare_post.left_list == compare_post.right_list:
            return (
                False,
                "Files in {a} do not match files in {b}",
                str(asset_dir),
                str(target_dir),
            )
    return (True, "Migrated assets from {a} to {b}", str(asset_dir), str(target_dir))


async def check_node_modules():
    """Check if node_modules exists and has contents.

    Returns:
        {bool} -- True if exists and has contents.
    """

    ui_path = Path(__file__).parent.parent / "ui"
    node_modules = ui_path / "node_modules"

    exists = node_modules.exists()
    valid = exists

    if exists and not tuple(node_modules.iterdir()):
        valid = False

    return valid


async def node_initial(dev_mode=False):
    """Initialize node_modules.

    Raises:
        RuntimeError: Raised if exit code is not 0
        RuntimeError: Raised if other exceptions occur

    Returns:
        {str} -- Command output
    """
    import asyncio

    ui_path = Path(__file__).parent.parent / "ui"

    mode = ""
    if not dev_mode:
        mode = "--prod"

    command = "yarn {m} --silent --emoji false".format(m=mode)

    all_messages = []
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd=command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=ui_path,
        )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        messages = stdout.decode("utf-8").strip()
        errors = stderr.decode("utf-8").strip()

        if proc.returncode != 0:
            raise RuntimeError(f"\nMessages:\n{messages}\nErrors:\n{errors}")

        await proc.wait()
        all_messages.append(messages)

    except Exception as e:
        raise RuntimeError(str(e))

    return "\n".join(all_messages)


async def read_package_json():
    """Import package.json as a python dict.

    Raises:
        RuntimeError: Raised if unable to read package.json

    Returns:
        {dict} -- NPM package.json as dict
    """

    import json

    package_json_file = Path(__file__).parent.parent / "ui" / "package.json"

    try:

        with package_json_file.open("r") as file:
            package_json = json.load(file)

    except Exception as e:
        raise RuntimeError(f"Error reading package.json: {str(e)}")

    log.debug("package.json:\n{p}", p=package_json)

    return package_json


def generate_opengraph(
    image_path: Path,
    max_width: int,
    max_height: int,
    target_path: Path,
    background_color: str,
):
    """Generate an OpenGraph compliant image."""
    from PIL import Image

    def center_point(background: Image, foreground: Image):
        """Generate a tuple of center points for PIL."""
        bg_x, bg_y = background.size[0:2]
        fg_x, fg_y = foreground.size[0:2]
        x1 = math.floor((bg_x / 2) - (fg_x / 2))
        y1 = math.floor((bg_y / 2) - (fg_y / 2))
        x2 = math.floor((bg_x / 2) + (fg_x / 2))
        y2 = math.floor((bg_y / 2) + (fg_y / 2))
        return (x1, y1, x2, y2)

    # Convert image to JPEG format with static name "opengraph.jpg"
    dst_path = target_path / "opengraph.jpg"

    # Copy the original image to the target path
    copied = shutil.copy2(image_path, target_path)
    log.debug("Copied {} to {}", str(image_path), str(target_path))

    with Image.open(copied) as src:

        # Only resize the image if it needs to be resized
        if src.size[0] != max_width or src.size[1] != max_height:

            # Resize image while maintaining aspect ratio
            log.debug("Opengraph image is not 1200x630, resizing...")
            src.thumbnail((max_width, max_height))

        # Only impose a background image if the original image has
        # alpha/transparency channels
        if src.mode in ("RGBA", "LA"):
            log.debug("Opengraph image has transparency, converting...")
            background = Image.new("RGB", (max_width, max_height), background_color)
            background.paste(src, box=center_point(background, src))
            dst = background
        else:
            dst = src

        # Save new image to derived target path
        dst.save(dst_path)

        # Delete the copied image
        Path(copied).unlink()

        if not dst_path.exists():
            raise RuntimeError(f"Unable to save resized image to {str(dst_path)}")

        log.debug("Opengraph image ready at {}", str(dst_path))

    return True


class FileCopy(Thread):
    """Custom thread for copyfiles() function."""

    def __init__(self, src: Path, dst: Path, queue: Queue):
        """Initialize custom thread."""
        super().__init__()

        if not src.exists():
            raise ValueError("{} does not exist", str(src))

        self.src = src
        self.dst = dst
        self.queue = queue

    def run(self):
        """Put one object into the queue for each file."""
        try:
            try:
                shutil.copy(self.src, self.dst)
            except IOError as err:
                self.queue.put(err)
            else:
                self.queue.put(self.src)
        finally:
            pass


def copyfiles(src_files: Iterable[Path], dst_files: Iterable[Path]):
    """Copy iterable of files from source to destination with threading."""
    queue = Queue()
    threads = ()
    src_files_len = len(src_files)
    dst_files_len = len(dst_files)

    if src_files_len != dst_files_len:
        raise ValueError(
            "The number of source files "
            + "({}) must match the number of destination files ({}).".format(
                src_files_len, dst_files_len
            )
        )

    for i, file in enumerate(src_files):
        file_thread = FileCopy(src=file, dst=dst_files[i], queue=queue)
        threads += (file_thread,)

    for thread in threads:
        thread.start()

    for _file in src_files:
        copied = queue.get()
        log.success("Copied {}", str(copied))

    for thread in threads:
        thread.join()

    for i, file in enumerate(dst_files):
        if not file.exists():
            raise RuntimeError("{} was not copied to {}", str(src_files[i]), str(file))

    return True


async def migrate_images(app_path: Path, params: dict):
    """Migrate images from source code to install directory."""
    images_dir = app_path / "static" / "images"
    favicon_dir = images_dir / "favicons"
    check_path(favicon_dir, create=True)
    src_files = ()
    dst_files = ()

    for image in ("light", "dark", "favicon"):
        src = Path(params["web"]["logo"][image])
        dst = images_dir / f"{image + src.suffix}"
        src_files += (src,)
        dst_files += (dst,)
    return copyfiles(src_files, dst_files)


async def build_frontend(  # noqa: C901
    dev_mode: bool,
    dev_url: str,
    prod_url: str,
    params: dict,
    app_path: Path,
    force: bool = False,
):
    """Perform full frontend UI build process.

    Securely creates temporary file, writes frontend configuration
    parameters to file as JSON. Then writes the name of the temporary
    file to /tmp/hyperglass.env.json as {"configFile": <file_name> }.

    Webpack reads /tmp/hyperglass.env.json, loads the temporary file,
    and sets its contents to Node environment variables during the build
    process.

    After the build is successful, the temporary file is automatically
    closed during garbage collection.

    Arguments:
        dev_mode {bool} -- Development Mode
        dev_url {str} -- Development Mode URL
        prod_url {str} -- Production Mode URL
        params {dict} -- Frontend Config paramters

    Raises:
        RuntimeError: Raised if errors occur during build process.

    Returns:
        {bool} -- True if successful
    """
    import hashlib
    import tempfile

    from aiofile import AIOFile
    import json
    from hyperglass.constants import __version__

    env_file = Path("/tmp/hyperglass.env.json")  # noqa: S108

    package_json = await read_package_json()

    env_vars = {
        "_HYPERGLASS_CONFIG_": params,
        "_HYPERGLASS_VERSION_": __version__,
        "_HYPERGLASS_PACKAGE_JSON_": package_json,
        "_HYPERGLASS_APP_PATH_": str(app_path),
    }

    # Set NextJS production/development mode and base URL based on
    # developer_mode setting.
    if dev_mode:
        env_vars.update({"NODE_ENV": "development", "_HYPERGLASS_URL_": dev_url})
    else:
        env_vars.update({"NODE_ENV": "production", "_HYPERGLASS_URL_": prod_url})

    # Check if hyperglass/ui/node_modules has been initialized. If not,
    # initialize it.
    initialized = await check_node_modules()
    if initialized:
        log.debug("node_modules is already initialized")
    elif not initialized:
        log.debug("node_modules has not been initialized. Starting initialization...")
        node_setup = await node_initial(dev_mode)
        if node_setup == "":
            log.debug("Re-initialized node_modules")

    try:
        env_json = json.dumps(env_vars, default=str)

        # Create SHA256 hash from all parameters passed to UI, use as
        # build identifier.
        build_id = hashlib.sha256(env_json.encode()).hexdigest()

        # Read hard-coded environment file from last build. If build ID
        # matches this build's ID, don't run a new build.
        if env_file.exists() and not force:
            async with AIOFile(env_file, "r") as ef:
                ef_json = await ef.read()
                ef_id = json.loads(ef_json).get("buildId", "empty")

                log.debug("Previous Build ID: {id}", id=ef_id)

                if ef_id == build_id:
                    log.debug(
                        "UI parameters unchanged since last build, skipping UI build..."
                    )
                    return True

        # Create temporary file. json file extension is added for easy
        # webpack JSON parsing.
        temp_file = tempfile.NamedTemporaryFile(
            mode="w+", prefix="hyperglass_", suffix=".json", delete=not dev_mode
        )
        log.info("Starting UI build...")
        log.debug(
            f"Created temporary UI config file: '{temp_file.name}' for build {build_id}"
        )

        async with AIOFile(temp_file.name, "w+") as temp:
            await temp.write(env_json)
            await temp.fsync()

            # Write "permanent" file (hard-coded named) for Node to read.
            async with AIOFile(env_file, "w+") as ef:
                await ef.write(
                    json.dumps({"configFile": temp_file.name, "buildId": build_id})
                )
                await ef.fsync()

                # While temporary file is still open, initiate UI build process.
                if not dev_mode or force:
                    initialize_result = await node_initial(dev_mode)
                    build_result = await build_ui(app_path=app_path)

                    if initialize_result:
                        log.debug(initialize_result)
                    elif initialize_result == "":
                        log.debug("Re-initialized node_modules")

                    if build_result:
                        log.success("Completed UI build")
                elif dev_mode and not force:
                    log.debug("Running in developer mode, did not build new UI files")

        await migrate_images(app_path, params)

        generate_opengraph(
            Path(params["web"]["opengraph"]["image"]),
            1200,
            630,
            app_path / "static" / "images",
            params["web"]["theme"]["colors"]["black"],
        )

    except Exception as e:
        raise RuntimeError(str(e)) from None

    return True


def set_app_path(required=False):
    """Find app directory and set value to environment variable."""
    import os

    from getpass import getuser

    matched_path = None

    config_paths = (Path.home() / "hyperglass", Path("/etc/hyperglass/"))

    for path in config_paths:
        try:
            if path.exists():
                tmp = path / "test.tmp"
                tmp.touch()
                if tmp.exists():
                    matched_path = path
                    tmp.unlink()
                    break
        except Exception:
            matched_path = None

    if required and matched_path is None:
        # Only raise an error if required is True
        raise RuntimeError(
            """
    No configuration directories were determined to both exist and be readable
    by hyperglass. hyperglass is running as user '{un}' (UID '{uid}'), and tried
    to access the following directories:
    {dir}""".format(
                un=getuser(),
                uid=os.getuid(),
                dir="\n".join([" - " + str(p) for p in config_paths]),
            )
        )

    os.environ["hyperglass_directory"] = str(matched_path)
    return True


def import_public_key(app_path, device_name, keystring):
    """Import a public key for hyperglass-agent.

    Arguments:
        app_path {Path|str} -- hyperglass app path
        device_name {str} -- Device name
        keystring {str} -- Public key

    Raises:
        RuntimeError: Raised if unable to create certs directory
        RuntimeError: Raised if written key does not match input

    Returns:
        {bool} -- True if file was written
    """
    if not isinstance(app_path, Path):
        app_path = Path(app_path)

    cert_dir = app_path / "certs"

    if not cert_dir.exists():
        cert_dir.mkdir()

    if not cert_dir.exists():
        raise RuntimeError(f"Failed to create certs directory at {str(cert_dir)}")

    filename = re.sub(r"[^A-Za-z0-9]", "_", device_name) + ".pem"
    cert_file = cert_dir / filename

    with cert_file.open("w+") as file:
        file.write(str(keystring))

    with cert_file.open("r") as file:
        read_file = file.read().strip()
        if not keystring == read_file:
            raise RuntimeError("Wrote key, but written file did not match input key")

    return True


def format_listen_address(listen_address):
    """Format a listen_address.

    Wraps IPv6 address in brackets.

    Arguments:
        listen_address {str} -- Preformatted listen_address

    Returns:
        {str} -- Formatted listen_address
    """
    from ipaddress import ip_address, IPv4Address, IPv6Address

    if isinstance(listen_address, str):
        try:
            listen_address = ip_address(listen_address)
            if listen_address.version == 6:
                listen_address = f"[{str(listen_address)}]"
            else:
                listen_address = str(listen_address)
        except ValueError:
            pass

    elif isinstance(listen_address, (IPv4Address, IPv6Address)):
        if listen_address.version == 6:
            listen_address = f"[{str(listen_address)}]"
        else:
            listen_address = str(listen_address)

    else:
        listen_address = str(listen_address)

    return listen_address


def split_on_uppercase(s):
    """Split characters by uppercase letters.

    From: https://stackoverflow.com/a/40382663

    """
    string_length = len(s)
    is_lower_around = (
        lambda: s[i - 1].islower() or string_length > (i + 1) and s[i + 1].islower()
    )

    start = 0
    parts = []
    for i in range(1, string_length):
        if s[i].isupper() and is_lower_around():
            parts.append(s[start:i])
            start = i
    parts.append(s[start:])

    return parts


def parse_exception(exc):
    """Parse an exception and its direct cause."""

    if not isinstance(exc, BaseException):
        raise TypeError(f"'{repr(exc)}' is not an exception.")

    def get_exc_name(exc):
        return " ".join(split_on_uppercase(exc.__class__.__name__))

    def get_doc_summary(doc):
        return doc.strip().split("\n")[0].strip(".")

    name = get_exc_name(exc)
    parsed = []
    if exc.__doc__:
        detail = get_doc_summary(exc.__doc__)
        parsed.append(f"{name} ({detail})")
    else:
        parsed.append(name)

    if exc.__cause__:
        cause = get_exc_name(exc.__cause__)
        if exc.__cause__.__doc__:
            cause_detail = get_doc_summary(exc.__cause__.__doc__)
            parsed.append(f"{cause} ({cause_detail})")
        else:
            parsed.append(cause)
    return ", caused by ".join(parsed)


def set_cache_env(host, port, db):
    """Set basic cache config parameters to environment variables.

    Functions using Redis to access the pickled config need to be able
    to access Redis without reading the config.
    """
    import os

    os.environ["HYPERGLASS_CACHE_HOST"] = str(host)
    os.environ["HYPERGLASS_CACHE_PORT"] = str(port)
    os.environ["HYPERGLASS_CACHE_DB"] = str(db)
    return True


def get_cache_env():
    """Get basic cache config from environment variables."""
    import os

    host = os.environ.get("HYPERGLASS_CACHE_HOST")
    port = os.environ.get("HYPERGLASS_CACHE_PORT")
    db = os.environ.get("HYPERGLASS_CACHE_DB")
    for i in (host, port, db):
        if i is None:
            raise LookupError(
                "Unable to find cache configuration in environment variables"
            )
    return host, port, db


async def process_headers(headers):
    """Filter out unwanted headers and return as a dictionary."""
    headers = dict(headers)
    header_keys = (
        "user-agent",
        "referer",
        "accept-encoding",
        "accept-language",
        "x-real-ip",
        "x-forwarded-for",
    )
    return {k: headers.get(k) for k in header_keys}


def make_repr(_class):
    """Create a user-friendly represention of an object."""
    from asyncio import iscoroutine

    def _process_attrs(_dir):
        for attr in _dir:
            if not attr.startswith("_"):
                attr_val = getattr(_class, attr)

                if callable(attr_val):
                    yield f'{attr}=<function name="{attr_val.__name__}">'

                elif iscoroutine(attr_val):
                    yield f'{attr}=<coroutine name="{attr_val.__name__}">'

                elif isinstance(attr_val, str):
                    yield f'{attr}="{attr_val}"'

                else:
                    yield f"{attr}={str(attr_val)}"

    return f'{_class.__name__}({", ".join(_process_attrs(dir(_class)))})'


def validate_nos(nos):
    """Validate device NOS is supported."""
    from hyperglass.constants import TRANSPORT_REST
    from netmiko.ssh_dispatcher import CLASS_MAPPER_BASE

    result = (False, None)

    if nos in TRANSPORT_REST:
        result = (True, "rest")
    elif nos in CLASS_MAPPER_BASE.keys():
        result = (True, "scrape")

    return result


def current_log_level(logger: LoguruLogger) -> str:
    """Get the current log level of a logger instance."""

    try:
        handler = list(logger._core.handlers.values())[0]
        levels = {v.no: k for k, v in logger._core.levels.items()}
        current_level = levels[handler.levelno].lower()

    except Exception as err:
        logger.error(err)
        current_level = "info"

    return current_level


def validation_error_message(*errors: Dict) -> str:
    """Parse errors return from pydantic.ValidationError.errors()."""

    errs = ("\n",)

    for err in errors:
        loc = " → ".join(str(loc) for loc in err["loc"])
        errs += (f'Field: {loc}\n  Error: {err["msg"]}\n',)

    return "\n".join(errs)
