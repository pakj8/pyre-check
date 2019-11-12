import json
import logging
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from time import sleep, time
from typing import Any, Dict, Iterator, List, Mapping, Optional, overload

from typing_extensions import Literal

from .environment import Environment
from .specification import BatchRepositoryUpdate, Specification


LOG: logging.Logger = logging.getLogger(__name__)


class MalformedPyreOutputException(Exception):
    pass


@dataclass
class PyreError:
    line: int
    column: int
    path: str
    description: str

    @staticmethod
    def from_json(input_json: Dict[str, Any]) -> "PyreError":
        try:
            return PyreError(
                line=int(input_json["line"]),
                column=int(input_json["column"]),
                path=input_json["path"],
                description=input_json["description"],
            )
        except KeyError as key:
            raise MalformedPyreOutputException(
                f"Cannot interpret pyre output: missing key '{key}'"
            )


class PyreRunner:
    def __init__(
        self,
        environment: Environment,
        specification: Specification,
        working_directory: Path,
    ) -> None:
        self._environment = environment
        self._specification = specification
        self._working_directory = working_directory
        invocation = self._environment.pyre_client_override or "pyre"
        binary_override = self._environment.pyre_binary_override
        if binary_override:
            invocation += f" --binary {binary_override}"
        typeshed_override = self._environment.typeshed_override
        if typeshed_override:
            invocation += f" --typeshed {typeshed_override}"
        self._pyre_invocation: str = invocation

    def update(self) -> List[Mapping[str, int]]:
        incremental_update_logs: List[Mapping[str, int]] = []
        new_state = self._specification.new_state
        updates = new_state.update_steps()
        for expected, update in enumerate(updates):
            update.update(self._environment, self._working_directory)
            while True:
                incremental_update_logs = self.run_profile("incremental_updates")
                if len(incremental_update_logs) > expected:
                    break
                else:
                    sleep(1)
        return incremental_update_logs

    def run_check(self) -> List[PyreError]:
        pyre_check_command = (
            f"{self._pyre_invocation} {self._specification.pyre_check_pyre_options} "
            "--output=json "
            "--noninteractive "
            f"check {self._specification.pyre_check_options}"
        ).rstrip()
        output = self._environment.checked_run(
            working_directory=self._working_directory,
            command=pyre_check_command,
            expected_return_codes=(0, 1),
        )
        if output.return_code == 0:
            return []
        else:
            return [PyreError.from_json(x) for x in json.loads(output.stdout)]

    def run_start(self) -> Mapping[str, int]:
        pyre_start_command = (
            # Use `pyre restart` instead of `pyre start` as the we want to:
            # - Kill existing servers
            # - Force the initial check to finish
            f"{self._pyre_invocation} {self._specification.pyre_start_pyre_options} "
            "--no-saved-state --enable-profiling "
            f"restart {self._specification.pyre_start_options}"
        ).rstrip()
        self._environment.checked_run(
            working_directory=self._working_directory, command=pyre_start_command
        )
        return self.run_profile("cold_start_phases")

    def run_stop(self) -> None:
        self._environment.checked_run(
            working_directory=self._working_directory,
            command=f"{self._pyre_invocation} stop",
        )

    def run_incremental(self) -> List[PyreError]:
        pyre_incremental_command = (
            f"{self._pyre_invocation} "
            f"{self._specification.pyre_incremental_pyre_options} "
            "--output=json "
            "--noninteractive "
            f"incremental {self._specification.pyre_incremental_options}"
        ).rstrip()
        output = self._environment.checked_run(
            working_directory=self._working_directory,
            command=pyre_incremental_command,
            expected_return_codes=(0, 1),
        )
        if output.return_code == 0:
            return []
        else:
            return [PyreError.from_json(x) for x in json.loads(output.stdout)]

    @overload
    def run_profile(
        self, output_kind: Literal["incremental_updates"]
    ) -> List[Mapping[str, int]]:
        ...

    @overload  # noqa T20027161
    def run_profile(
        self, output_kind: Literal["cold_start_phases"]
    ) -> Mapping[str, int]:
        ...

    def run_profile(self, output_kind: str) -> object:  # noqa T20027161
        pyre_profile_command = (
            f"{self._pyre_invocation} " f"profile --output={output_kind}"
        ).rstrip()
        output = self._environment.checked_run(
            working_directory=self._working_directory, command=pyre_profile_command
        )
        return json.loads(output.stdout)


@contextmanager
def _create_pyre_runner(
    environment: Environment, specification: Specification
) -> Iterator["PyreRunner"]:
    with specification.old_state.activate_sandbox(environment) as sandbox_root:
        yield PyreRunner(environment, specification, sandbox_root)


@dataclass
class InconsistentOutput:
    full_check_output: List[PyreError]
    incremental_check_output: List[PyreError]

    def to_json(self) -> Dict[str, Any]:
        return {
            "full_check_output": [asdict(e) for e in self.full_check_output],
            "incremental_check_output": [
                asdict(e) for e in self.incremental_check_output
            ],
        }


@dataclass
class ProfileLogs:
    incremental_update_logs: List[Mapping[str, int]]
    cold_start_log: Mapping[str, int]

    def to_json(self) -> Dict[str, Any]:
        return {
            "incremental_update_logs": self.incremental_update_logs,
            "cold_start_log": self.cold_start_log,
        }

    def total_of_totals(self) -> int:
        return sum(log["total"] for log in self.incremental_update_logs)


@dataclass
class ResultComparison:
    discrepancy: Optional[InconsistentOutput]
    full_check_time: int
    profile_logs: ProfileLogs

    def to_json(self, dont_show_discrepancy: bool = False) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "full_check_time": self.full_check_time,
            "incremental_check_time": self.profile_logs.total_of_totals(),
            "profile_logs": self.profile_logs.to_json(),
        }
        discrepancy = self.discrepancy
        if dont_show_discrepancy:
            return result
        else:
            result["discrepancy"] = (
                "none" if discrepancy is None else discrepancy.to_json()
            )
            return result


def compare_server_to_full(
    environment: Environment, specification: Specification
) -> ResultComparison:
    LOG.info("Preparing base repository state...")
    with _create_pyre_runner(environment, specification) as pyre_runner:
        LOG.debug("Starting pyre server...")
        cold_start_log = pyre_runner.run_start()
        LOG.debug("Preparing updated repository state...")
        incremental_update_logs = pyre_runner.update()

        LOG.info("Running pyre incremental check...")
        incremental_check_output = pyre_runner.run_incremental()
        LOG.debug(f"Stopping pyre server...")
        pyre_runner.run_stop()
        LOG.info(
            f"Pyre incremental check successfully finished (with {len(incremental_check_output)} errors)."  # noqa: line too long
        )

        LOG.info("Running pyre full check...")
        start_time = time()
        full_check_output = pyre_runner.run_check()
        full_check_time = int((time() - start_time) * 1000)
        LOG.info(
            f"Pyre full check successfully finished (with {len(full_check_output)} errors)."  # noqa: line too long
        )

    discrepancy = (
        None
        if incremental_check_output == full_check_output
        else InconsistentOutput(full_check_output, incremental_check_output)
    )
    profile_logs = ProfileLogs(incremental_update_logs, cold_start_log)
    return ResultComparison(discrepancy, full_check_time, profile_logs)


def benchmark_server(
    environment: Environment, specification: Specification
) -> ProfileLogs:
    LOG.info("Preparing base repository state...")
    with _create_pyre_runner(environment, specification) as pyre_runner:
        LOG.debug("Starting pyre server...")
        cold_start_log = pyre_runner.run_start()
        LOG.debug("Preparing updated repository state...")
        incremental_update_logs = pyre_runner.update()

        LOG.info("Running pyre incremental check...")
        incremental_check_output = pyre_runner.run_incremental()
        LOG.debug(f"Stopping pyre server...")
        pyre_runner.run_stop()
        LOG.info(
            f"Pyre incremental check successfully finished (with {len(incremental_check_output)} errors)."  # noqa: line too long
        )
    return ProfileLogs(incremental_update_logs, cold_start_log)