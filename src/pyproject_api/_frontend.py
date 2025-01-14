"""Build frontend for PEP-517"""
import json
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import sleep
from typing import Any, Dict, Iterator, List, NamedTuple, NoReturn, Optional, Tuple, cast
from zipfile import ZipFile

import tomli
from packaging.requirements import Requirement

from pyproject_api._util import ensure_empty_dir

_HERE = Path(__file__).parent
ConfigSettings = Optional[Dict[str, Any]]


class CmdStatus(ABC):
    @property
    @abstractmethod
    def done(self) -> bool:
        """:return: truthful when the command finished running"""
        raise NotImplementedError

    @abstractmethod
    def out_err(self) -> Tuple[str, str]:
        """:return: standard output and standard error text"""
        raise NotImplementedError


class RequiresBuildSdistResult(NamedTuple):
    """Information collected while acquiring the source distribution build dependencies"""

    #: wheel build dependencies
    requires: Tuple[Requirement, ...]
    #: backend standard output while acquiring the source distribution build dependencies
    out: str
    #: backend standard output while acquiring the source distribution build dependencies
    err: str


class RequiresBuildWheelResult(NamedTuple):
    """Information collected while acquiring the wheel build dependencies"""

    #: wheel build dependencies
    requires: Tuple[Requirement, ...]
    #: backend standard output while acquiring the wheel build dependencies
    out: str
    #: backend standard error while acquiring the wheel build dependencies
    err: str


class MetadataForBuildWheelResult(NamedTuple):
    """Information collected while acquiring the wheel metadata"""

    #: path to the wheel metadata
    metadata: Path
    #: backend standard output while generating the wheel metadata
    out: str
    #: backend standard output while generating the wheel metadata
    err: str


class SdistResult(NamedTuple):
    """Information collected while building a source distribution"""

    #: path to the built source distribution
    sdist: Path
    #: backend standard output while building the source distribution
    out: str
    #: backend standard output while building the source distribution
    err: str


class WheelResult(NamedTuple):
    """Information collected while building a wheel"""

    #: path to the built wheel artifact
    wheel: Path
    #: backend standard output while building the wheel
    out: str
    #: backend standard error while building the wheel
    err: str


class BackendFailed(RuntimeError):
    """An error of the build backend."""

    def __init__(self, result: Dict[str, Any], out: str, err: str) -> None:
        super().__init__()
        #: standard output collected while running the command
        self.out = out
        #: standard error collected while running the command
        self.err = err
        #: exit code of the command
        self.code: int = result.get("code", -2)
        #: the type of exception thrown
        self.exc_type: str = result.get("exc_type", "missing Exception type")
        #: the string representation of the exception thrown
        self.exc_msg: str = result.get("exc_msg", "missing Exception message")

    def __str__(self) -> str:
        return (
            f"packaging backend failed{'' if self.code is None else f' (code={self.code})'}, "
            f"with {self.exc_type}: {self.exc_msg}\n{self.err}{self.out}"
        ).rstrip()

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"result=dict(code={self.code}, exc_type={self.exc_type!r},exc_msg={self.exc_msg!r}),"
            f" out={self.out!r}, err={self.err!r})"
        )


class Frontend(ABC):
    """
    Abstract base class for a pyproject frontend.
    """

    #: backend key when the ``pyproject.toml`` does not specify it
    LEGACY_BUILD_BACKEND: str = "setuptools.build_meta:__legacy__"
    #: backend requirements when the ``pyproject.toml`` does not specify it
    LEGACY_REQUIRES: Tuple[Requirement, ...] = (Requirement("setuptools >= 40.8.0"), Requirement("wheel"))

    def __init__(
        self,
        root: Path,
        backend_paths: Tuple[Path, ...],
        backend_module: str,
        backend_obj: Optional[str],
        requires: Tuple[Requirement, ...],
        reuse_backend: bool = True,
    ) -> None:
        """
        Create a new frontend.

        :param root: the root path of the project
        :param backend_paths: paths to provision as available to import from for the build backend
        :param backend_module: the module where the backend lives
        :param backend_obj: the backend object key (will be lookup up within the backend module)
        :param requires: build requirements for the backend
        :param reuse_backend: a flag indicating if the communication channel should be kept alive between messages
        """
        self._root = root
        self._backend_paths = backend_paths
        self._backend_module = backend_module
        self._backend_obj = backend_obj
        self.requires: Tuple[Requirement, ...] = requires
        self._reuse_backend = reuse_backend

    @classmethod
    def create_args_from_folder(
        cls, folder: Path
    ) -> Tuple[Path, Tuple[Path, ...], str, Optional[str], Tuple[Requirement, ...], bool]:
        """
        Frontend creation arguments from a python project folder (thould have a ``pypyproject.toml`` file per PEP-518).

        :param folder: the python project folder
        :return: the frontend creation args

        E.g., to create a frontend from a python project folder:

        .. code:: python

            frontend = Frontend(*Frontend.create_args_from_folder(project_folder))
        """
        py_project_toml = folder / "pyproject.toml"
        if py_project_toml.exists():
            with py_project_toml.open("rb") as file_handler:
                py_project = tomli.load(file_handler)
            build_system = py_project.get("build-system", {})
            if "backend-path" in build_system:
                backend_paths: Tuple[Path, ...] = tuple(folder / p for p in build_system["backend-path"])
            else:
                backend_paths = ()
            if "requires" in build_system:
                requires: Tuple[Requirement, ...] = tuple(Requirement(r) for r in build_system.get("requires"))
            else:
                requires = cls.LEGACY_REQUIRES
            build_backend = build_system.get("build-backend", cls.LEGACY_BUILD_BACKEND)
        else:
            backend_paths = ()
            requires = cls.LEGACY_REQUIRES
            build_backend = cls.LEGACY_BUILD_BACKEND
        paths = build_backend.split(":")
        backend_module: str = paths[0]
        backend_obj: Optional[str] = paths[1] if len(paths) > 1 else None
        return folder, backend_paths, backend_module, backend_obj, requires, True

    @property
    def backend(self) -> str:
        """:return: backend key"""
        return f"{self._backend_module}{f':{self._backend_obj}' if self._backend_obj else ''}"

    @property
    def backend_args(self) -> List[str]:
        """:return: startup arguments for a backend"""
        result: List[str] = [str(_HERE / "_backend.py"), str(self._reuse_backend), self._backend_module]
        if self._backend_obj:
            result.append(self._backend_obj)
        return result

    def get_requires_for_build_sdist(
        self, config_settings: Optional[ConfigSettings] = None
    ) -> RequiresBuildSdistResult:
        """
        Get build requirements for a source distribution (per PEP-517).

        :param config_settings: run arguments
        :return: outcome
        """
        try:
            result, out, err = self._send(cmd="get_requires_for_build_sdist", config_settings=config_settings)
        except BackendFailed as exc:
            result, out, err = [], exc.out, exc.err
        if not isinstance(result, list) or not all(isinstance(i, str) for i in result):
            self._unexpected_response("get_requires_for_build_sdist", result, "list of string", out, err)
        return RequiresBuildSdistResult(tuple(Requirement(r) for r in cast(List[str], result)), out, err)

    def get_requires_for_build_wheel(
        self, config_settings: Optional[ConfigSettings] = None
    ) -> RequiresBuildWheelResult:
        """
        Get build requirements for a wheel (per PEP-517).

        :param config_settings: run arguments
        :return: outcome
        """
        try:
            result, out, err = self._send(cmd="get_requires_for_build_wheel", config_settings=config_settings)
        except BackendFailed as exc:
            result, out, err = [], exc.out, exc.err
        if not isinstance(result, list) or not all(isinstance(i, str) for i in result):
            self._unexpected_response("get_requires_for_build_wheel", result, "list of string", out, err)
        return RequiresBuildWheelResult(tuple(Requirement(r) for r in cast(List[str], result)), out, err)

    def prepare_metadata_for_build_wheel(
        self, metadata_directory: Path, config_settings: Optional[ConfigSettings] = None
    ) -> MetadataForBuildWheelResult:
        """
        Build wheel metadata (per PEP-517).

        :param metadata_directory: where to generate the metadata
        :param config_settings: build arguments
        :return: metadata generation result
        """
        if metadata_directory == self._root:
            raise RuntimeError(f"the project root and the metadata directory can't be the same {self._root}")
        if metadata_directory.exists():  # start with fresh
            ensure_empty_dir(metadata_directory)
        metadata_directory.mkdir(parents=True, exist_ok=True)
        try:
            basename, out, err = self._send(
                cmd="prepare_metadata_for_build_wheel",
                metadata_directory=metadata_directory,
                config_settings=config_settings,
            )
        except BackendFailed:
            # if backend does not provide it acquire it from the wheel
            basename, err, out = self._metadata_from_built_wheel(config_settings, metadata_directory)
        if not isinstance(basename, str):
            self._unexpected_response("prepare_metadata_for_build_wheel", basename, str, out, err)
        result = metadata_directory / basename
        return MetadataForBuildWheelResult(result, out, err)

    def build_sdist(self, sdist_directory: Path, config_settings: Optional[ConfigSettings] = None) -> SdistResult:
        """
        Build a source distribution (per PEP-517).

        :param sdist_directory: the folder where to build the source distribution
        :param config_settings: build arguments
        :return: source distribution build result
        """
        sdist_directory.mkdir(parents=True, exist_ok=True)
        basename, out, err = self._send(
            cmd="build_sdist",
            sdist_directory=sdist_directory,
            config_settings=config_settings,
        )
        if not isinstance(basename, str):
            self._unexpected_response("build_sdist", basename, str, out, err)
        return SdistResult(sdist_directory / basename, out, err)

    def build_wheel(
        self,
        wheel_directory: Path,
        config_settings: Optional[ConfigSettings] = None,
        metadata_directory: Optional[Path] = None,
    ) -> WheelResult:
        """
        Build a source distribution (per PEP-517).

        :param wheel_directory: the folder where to build the wheel
        :param config_settings: build arguments
        :param metadata_directory: wheel metadata folder
        :return: wheel build result
        """
        wheel_directory.mkdir(parents=True, exist_ok=True)
        basename, out, err = self._send(
            cmd="build_wheel",
            wheel_directory=wheel_directory,
            config_settings=config_settings,
            metadata_directory=metadata_directory,
        )
        if not isinstance(basename, str):
            self._unexpected_response("build_wheel", basename, str, out, err)
        return WheelResult(wheel_directory / basename, out, err)

    def _unexpected_response(self, cmd: str, got: Any, expected_type: Any, out: str, err: str) -> NoReturn:
        msg = f"{cmd!r} on {self.backend!r} returned {got!r} but expected type {expected_type!r}"
        raise BackendFailed({"code": None, "exc_type": TypeError.__name__, "exc_msg": msg}, out, err)

    def _metadata_from_built_wheel(
        self, config_settings: Optional[ConfigSettings], metadata_directory: Optional[Path]
    ) -> Tuple[str, str, str]:
        with self._wheel_directory() as wheel_directory:
            wheel_result = self.build_wheel(
                wheel_directory=wheel_directory,
                config_settings=config_settings,
                metadata_directory=metadata_directory,
            )
            wheel = wheel_result.wheel
            if not wheel.exists():
                raise RuntimeError(f"missing wheel file return by backed {wheel!r}")
            out, err = wheel_result.out, wheel_result.err
            extract_to = str(metadata_directory)
            basename = None
            with ZipFile(str(wheel), "r") as zip_file:
                for name in zip_file.namelist():  # pragma: no branch
                    path = Path(name)
                    if path.parts[0].endswith(".dist-info"):
                        basename = path.parts[0]
                        zip_file.extract(name, extract_to)
            if basename is None:  # pragma: no branch
                raise RuntimeError(f"no .dist-info found inside generated wheel {wheel}")
        return basename, err, out

    @contextmanager
    def _wheel_directory(self) -> Iterator[Path]:
        with TemporaryDirectory() as wheel_directory:
            yield Path(wheel_directory)

    def _send(self, cmd: str, **kwargs: Any) -> Tuple[Any, str, str]:
        with NamedTemporaryFile(prefix=f"pep517_{cmd}-") as result_file_marker:
            result_file = Path(result_file_marker.name).with_suffix(".json")
            msg = json.dumps(
                {
                    "cmd": cmd,
                    "kwargs": {k: (str(v) if isinstance(v, Path) else v) for k, v in kwargs.items()},
                    "result": str(result_file),
                }
            )
            with self._send_msg(cmd, result_file, msg) as status:
                while not status.done:  # pragma: no branch
                    sleep(0.001)  # wait a bit for things to happen
            if result_file.exists():
                try:
                    with result_file.open("rt") as result_handler:
                        result = json.load(result_handler)
                finally:
                    result_file.unlink()
            else:
                result = {
                    "code": 1,
                    "exc_type": "RuntimeError",
                    "exc_msg": f"Backend response file {result_file} is missing",
                }
        out, err = status.out_err()
        if "return" in result:
            return result["return"], out, err
        raise BackendFailed(result, out, err)

    @abstractmethod
    @contextmanager
    def _send_msg(self, cmd: str, result_file: Path, msg: str) -> Iterator[CmdStatus]:
        raise NotImplementedError
