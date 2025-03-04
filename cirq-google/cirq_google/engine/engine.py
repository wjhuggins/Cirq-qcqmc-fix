# Copyright 2018 The Cirq Developers
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Classes for running against Google's Quantum Cloud Service.

As an example, to run a circuit against the xmon simulator on the cloud,
    engine = cirq_google.Engine(project_id='my-project-id')
    program = engine.create_program(circuit)
    result0 = program.run(params=params0, repetitions=10)
    result1 = program.run(params=params1, repetitions=10)

In order to run on must have access to the Quantum Engine API. Access to this
API is (as of June 22, 2018) restricted to invitation only.
"""

import datetime
import enum
import os
import random
import string
from typing import Dict, Iterable, List, Optional, Sequence, Set, TypeVar, Union, TYPE_CHECKING

from google.protobuf import any_pb2

import cirq
from cirq_google.api import v2
from cirq_google.engine import engine_client
from cirq_google.engine.client import quantum
from cirq_google.engine.result_type import ResultType
from cirq_google.serialization import SerializableGateSet, Serializer
from cirq_google.serialization.arg_func_langs import arg_to_proto
from cirq_google.engine import (
    engine_client,
    engine_program,
    engine_job,
    engine_processor,
    engine_sampler,
)

if TYPE_CHECKING:
    import cirq_google
    import google.protobuf

TYPE_PREFIX = 'type.googleapis.com/'

_R = TypeVar('_R')


class ProtoVersion(enum.Enum):
    """Protocol buffer version to use for requests to the quantum engine."""

    UNDEFINED = 0
    V1 = 1
    V2 = 2


def _make_random_id(prefix: str, length: int = 16):
    random_digits = [random.choice(string.ascii_uppercase + string.digits) for _ in range(length)]
    suffix = ''.join(random_digits)
    suffix += datetime.datetime.now().strftime('%y%m%d-%H%M%S')
    return f'{prefix}{suffix}'


@cirq.value_equality
class EngineContext:
    """Context for running against the Quantum Engine API. Most users should
    simply create an Engine object instead of working with one of these
    directly."""

    def __init__(
        self,
        proto_version: Optional[ProtoVersion] = None,
        service_args: Optional[Dict] = None,
        verbose: Optional[bool] = None,
        client: 'Optional[engine_client.EngineClient]' = None,
        timeout: Optional[int] = None,
    ) -> None:
        """Context and client for using Quantum Engine.

        Args:
            proto_version: The version of cirq protos to use. If None, then
                ProtoVersion.V2 will be used.
            service_args: A dictionary of arguments that can be used to
                configure options on the underlying client.
            verbose: Suppresses stderr messages when set to False. Default is
                true.
            timeout: Timeout for polling for results, in seconds.  Default is
                to never timeout.
        """
        if (service_args or verbose) and client:
            raise ValueError('either specify service_args and verbose or client')

        self.proto_version = proto_version or ProtoVersion.V2
        if self.proto_version == ProtoVersion.V1:
            raise ValueError('ProtoVersion V1 no longer supported')

        if not client:
            client = engine_client.EngineClient(service_args=service_args, verbose=verbose)
        self.client = client
        self.timeout = timeout

    def copy(self) -> 'EngineContext':
        return EngineContext(proto_version=self.proto_version, client=self.client)

    def _value_equality_values_(self):
        return self.proto_version, self.client


class Engine:
    """Runs programs via the Quantum Engine API.

    This class has methods for creating programs and jobs that execute on
    Quantum Engine:
        create_program
        run
        run_sweep
        run_batch

    Another set of methods return information about programs and jobs that
    have been previously created on the Quantum Engine, as well as metadata
    about available processors:
        get_program
        list_processors
        get_processor
    """

    def __init__(
        self,
        project_id: str,
        proto_version: Optional[ProtoVersion] = None,
        service_args: Optional[Dict] = None,
        verbose: Optional[bool] = None,
        timeout: Optional[int] = None,
        context: Optional[EngineContext] = None,
    ) -> None:
        """Supports creating and running programs against the Quantum Engine.

        Args:
            project_id: A project_id string of the Google Cloud Project to use.
                API interactions will be attributed to this project and any
                resources created will be owned by the project. See
                https://cloud.google.com/resource-manager/docs/creating-managing-projects#identifying_projects
            proto_version: The version of cirq protos to use. If None, then
                ProtoVersion.V2 will be used.
            service_args: A dictionary of arguments that can be used to
                configure options on the underlying client.
            verbose: Suppresses stderr messages when set to False. Default is
                true.
            timeout: Timeout for polling for results, in seconds.  Default is
                to never timeout.
            context: Engine configuration and context to use. For most users
                this should never be specified.
        """
        if context and (proto_version or service_args or verbose):
            raise ValueError('Either provide context or proto_version, service_args and verbose.')

        self.project_id = project_id
        if not context:
            context = EngineContext(
                proto_version=proto_version,
                service_args=service_args,
                verbose=verbose,
                timeout=timeout,
            )
        self.context = context

    def __str__(self) -> str:
        return f'Engine(project_id={self.project_id!r})'

    def run(
        self,
        program: cirq.Circuit,
        program_id: Optional[str] = None,
        job_id: Optional[str] = None,
        param_resolver: cirq.ParamResolver = cirq.ParamResolver({}),
        repetitions: int = 1,
        processor_ids: Sequence[str] = ('xmonsim',),
        gate_set: Optional[Serializer] = None,
        program_description: Optional[str] = None,
        program_labels: Optional[Dict[str, str]] = None,
        job_description: Optional[str] = None,
        job_labels: Optional[Dict[str, str]] = None,
    ) -> cirq.Result:
        """Runs the supplied Circuit via Quantum Engine.

        Args:
            program: The Circuit to execute. If a circuit is
                provided, a moment by moment schedule will be used.
            program_id: A user-provided identifier for the program. This must
                be unique within the Google Cloud project being used. If this
                parameter is not provided, a random id of the format
                'prog-################YYMMDD' will be generated, where # is
                alphanumeric and YYMMDD is the current year, month, and day.
            job_id: Job identifier to use. If this is not provided, a random id
                of the format 'job-################YYMMDD' will be generated,
                where # is alphanumeric and YYMMDD is the current year, month,
                and day.
            param_resolver: Parameters to run with the program.
            repetitions: The number of repetitions to simulate.
            processor_ids: The engine processors that should be candidates
                to run the program. Only one of these will be scheduled for
                execution.
            gate_set: The gate set used to serialize the circuit. The gate set
                must be supported by the selected processor.
            program_description: An optional description to set on the program.
            program_labels: Optional set of labels to set on the program.
            job_description: An optional description to set on the job.
            job_labels: Optional set of labels to set on the job.

        Returns:
            A single Result for this run.
        """
        if not gate_set:
            raise ValueError('No gate set provided')
        return list(
            self.run_sweep(
                program=program,
                program_id=program_id,
                job_id=job_id,
                params=[param_resolver],
                repetitions=repetitions,
                processor_ids=processor_ids,
                gate_set=gate_set,
                program_description=program_description,
                program_labels=program_labels,
                job_description=job_description,
                job_labels=job_labels,
            )
        )[0]

    def run_sweep(
        self,
        program: cirq.Circuit,
        program_id: Optional[str] = None,
        job_id: Optional[str] = None,
        params: cirq.Sweepable = None,
        repetitions: int = 1,
        processor_ids: Sequence[str] = ('xmonsim',),
        gate_set: Optional[Serializer] = None,
        program_description: Optional[str] = None,
        program_labels: Optional[Dict[str, str]] = None,
        job_description: Optional[str] = None,
        job_labels: Optional[Dict[str, str]] = None,
    ) -> engine_job.EngineJob:
        """Runs the supplied Circuit via Quantum Engine.Creates

        In contrast to run, this runs across multiple parameter sweeps, and
        does not block until a result is returned.

        Args:
            program: The Circuit to execute. If a circuit is
                provided, a moment by moment schedule will be used.
            program_id: A user-provided identifier for the program. This must
                be unique within the Google Cloud project being used. If this
                parameter is not provided, a random id of the format
                'prog-################YYMMDD' will be generated, where # is
                alphanumeric and YYMMDD is the current year, month, and day.
            job_id: Job identifier to use. If this is not provided, a random id
                of the format 'job-################YYMMDD' will be generated,
                where # is alphanumeric and YYMMDD is the current year, month,
                and day.
            params: Parameters to run with the program.
            repetitions: The number of circuit repetitions to run.
            processor_ids: The engine processors that should be candidates
                to run the program. Only one of these will be scheduled for
                execution.
            gate_set: The gate set used to serialize the circuit. The gate set
                must be supported by the selected processor.
            program_description: An optional description to set on the program.
            program_labels: Optional set of labels to set on the program.
            job_description: An optional description to set on the job.
            job_labels: Optional set of labels to set on the job.

        Returns:
            An EngineJob. If this is iterated over it returns a list of
            TrialResults, one for each parameter sweep.
        """
        if not gate_set:
            raise ValueError('No gate set provided')
        engine_program = self.create_program(
            program, program_id, gate_set, program_description, program_labels
        )
        return engine_program.run_sweep(
            job_id=job_id,
            params=params,
            repetitions=repetitions,
            processor_ids=processor_ids,
            description=job_description,
            labels=job_labels,
        )

    def run_batch(
        self,
        programs: List[cirq.Circuit],
        program_id: Optional[str] = None,
        job_id: Optional[str] = None,
        params_list: List[cirq.Sweepable] = None,
        repetitions: int = 1,
        processor_ids: Sequence[str] = (),
        gate_set: Optional[Serializer] = None,
        program_description: Optional[str] = None,
        program_labels: Optional[Dict[str, str]] = None,
        job_description: Optional[str] = None,
        job_labels: Optional[Dict[str, str]] = None,
    ) -> engine_job.EngineJob:
        """Runs the supplied Circuits via Quantum Engine.Creates

        This will combine each Circuit provided in `programs` into
        a BatchProgram.  Each circuit will pair with the associated
        parameter sweep provided in the `params_list`.  The number of
        programs is required to match the number of sweeps.

        This method does not block until a result is returned.  However,
        no results will be available until the entire batch is complete.

        Args:
            programs: The Circuits to execute as a batch.
            program_id: A user-provided identifier for the program. This must
                be unique within the Google Cloud project being used. If this
                parameter is not provided, a random id of the format
                'prog-################YYMMDD' will be generated, where # is
                alphanumeric and YYMMDD is the current year, month, and day.
            job_id: Job identifier to use. If this is not provided, a random id
                of the format 'job-################YYMMDD' will be generated,
                where # is alphanumeric and YYMMDD is the current year, month,
                and day.
            params_list: Parameter sweeps to use with the circuits. The number
                of sweeps should match the number of circuits and will be
                paired in order with the circuits. If this is None, it is
                assumed that the circuits are not parameterized and do not
                require sweeps.
            repetitions: Number of circuit repetitions to run.  Each sweep value
                of each circuit in the batch will run with the same repetitions.
            processor_ids: The engine processors that should be candidates
                to run the program. Only one of these will be scheduled for
                execution.
            gate_set: The gate set used to serialize the circuit. The gate set
                must be supported by the selected processor.
            program_description: An optional description to set on the program.
            program_labels: Optional set of labels to set on the program.
            job_description: An optional description to set on the job.
            job_labels: Optional set of labels to set on the job.

        Returns:
            An EngineJob. If this is iterated over it returns a list of
            TrialResults. All TrialResults for the first circuit are listed
            first, then the TrialResults for the second, etc. The TrialResults
            for a circuit are listed in the order imposed by the associated
            parameter sweep.
        """
        if params_list is None:
            params_list = [None] * len(programs)
        elif len(programs) != len(params_list):
            raise ValueError('Number of circuits and sweeps must match')
        if not processor_ids:
            raise ValueError('Processor id must be specified.')
        engine_program = self.create_batch_program(
            programs, program_id, gate_set, program_description, program_labels
        )
        return engine_program.run_batch(
            job_id=job_id,
            params_list=params_list,
            repetitions=repetitions,
            processor_ids=processor_ids,
            description=job_description,
            labels=job_labels,
        )

    def run_calibration(
        self,
        layers: List['cirq_google.CalibrationLayer'],
        program_id: Optional[str] = None,
        job_id: Optional[str] = None,
        processor_id: str = None,
        processor_ids: Sequence[str] = (),
        gate_set: Optional[Serializer] = None,
        program_description: Optional[str] = None,
        program_labels: Optional[Dict[str, str]] = None,
        job_description: Optional[str] = None,
        job_labels: Optional[Dict[str, str]] = None,
    ) -> engine_job.EngineJob:
        """Runs the specified calibrations via the Calibration API.

        Each calibration will be specified by a `CalibrationLayer`
        that contains the type of the calibrations to run, a `Circuit`
        to optimize, and any arguments needed by the calibration routine.

        Arguments and circuits needed for each layer will vary based on the
        calibration type.  However, the typical calibration routine may
        require a single moment defining the gates to optimize, for example.

        Note: this is an experimental API and is not yet fully supported
        for all users.

        Args:
            layers: The layers of calibration to execute as a batch.
            program_id: A user-provided identifier for the program. This must
                be unique within the Google Cloud project being used. If this
                parameter is not provided, a random id of the format
                'calibration-################YYMMDD' will be generated,
                where # is alphanumeric and YYMMDD is the current year, month,
                and day.
            job_id: Job identifier to use. If this is not provided, a random id
                of the format 'calibration-################YYMMDD' will be
                generated, where # is alphanumeric and YYMMDD is the current
                year, month, and day.
            processor_id: The engine processor that should run the calibration.
                If this is specified, processor_ids should not be specified.
            processor_ids: The engine processors that should be candidates
                to run the program. Only one of these will be scheduled for
                execution.
            gate_set: The gate set used to serialize the circuit. The gate set
                must be supported by the selected processor.
            program_description: An optional description to set on the program.
            program_labels: Optional set of labels to set on the program.
            job_description: An optional description to set on the job.
            job_labels: Optional set of labels to set on the job.  By defauly,
                this will add a 'calibration' label to the job.

        Returns:
            An EngineJob whose results can be retrieved by calling
            calibration_results().
        """
        if processor_id and processor_ids:
            raise ValueError('Only one of processor_id and processor_ids can be specified.')
        if not processor_ids and not processor_id:
            raise ValueError('Processor id must be specified.')
        if processor_id:
            processor_ids = [processor_id]
        if job_labels is None:
            job_labels = {'calibration': ''}
        engine_program = self.create_calibration_program(
            layers, program_id, gate_set, program_description, program_labels
        )
        return engine_program.run_calibration(
            job_id=job_id,
            processor_ids=processor_ids,
            description=job_description,
            labels=job_labels,
        )

    def create_program(
        self,
        program: cirq.Circuit,
        program_id: Optional[str] = None,
        gate_set: Optional[Serializer] = None,
        description: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
    ) -> engine_program.EngineProgram:
        """Wraps a Circuit for use with the Quantum Engine.

        Args:
            program: The Circuit to execute.
            program_id: A user-provided identifier for the program. This must be
                unique within the Google Cloud project being used. If this
                parameter is not provided, a random id of the format
                'prog-################YYMMDD' will be generated, where # is
                alphanumeric and YYMMDD is the current year, month, and day.
            gate_set: The gate set used to serialize the circuit. The gate set
                must be supported by the selected processor
            description: An optional description to set on the program.
            labels: Optional set of labels to set on the program.

        Returns:
            A EngineProgram for the newly created program.
        """
        if not gate_set:
            raise ValueError('No gate set provided')

        if not program_id:
            program_id = _make_random_id('prog-')

        new_program_id, new_program = self.context.client.create_program(
            self.project_id,
            program_id,
            code=self._serialize_program(program, gate_set),
            description=description,
            labels=labels,
        )

        return engine_program.EngineProgram(
            self.project_id, new_program_id, self.context, new_program
        )

    def create_batch_program(
        self,
        programs: List[cirq.Circuit],
        program_id: Optional[str] = None,
        gate_set: Optional[Serializer] = None,
        description: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
    ) -> engine_program.EngineProgram:
        """Wraps a list of Circuits into a BatchProgram for the Quantum Engine.

        Args:
            programs: The Circuits to execute within a batch.
            program_id: A user-provided identifier for the program. This must be
                unique within the Google Cloud project being used. If this
                parameter is not provided, a random id of the format
                'prog-################YYMMDD' will be generated, where # is
                alphanumeric and YYMMDD is the current year, month, and day.
            gate_set: The gate set used to serialize the circuit. The gate set
                must be supported by the selected processor
            description: An optional description to set on the program.
            labels: Optional set of labels to set on the program.

        Returns:
            A EngineProgram for the newly created program.
        """
        if not gate_set:
            raise ValueError('Gate set must be specified.')
        if not program_id:
            program_id = _make_random_id('prog-')

        batch = v2.batch_pb2.BatchProgram()
        for program in programs:
            gate_set.serialize(program, msg=batch.programs.add())

        new_program_id, new_program = self.context.client.create_program(
            self.project_id,
            program_id,
            code=self._pack_any(batch),
            description=description,
            labels=labels,
        )

        return engine_program.EngineProgram(
            self.project_id, new_program_id, self.context, new_program, result_type=ResultType.Batch
        )

    def create_calibration_program(
        self,
        layers: List['cirq_google.CalibrationLayer'],
        program_id: Optional[str] = None,
        gate_set: Optional[Serializer] = None,
        description: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
    ) -> engine_program.EngineProgram:
        """Wraps a list of calibration layers into an Any for Quantum Engine.

        Args:
            layers: The calibration routines to execute.  All layers will be
                executed within the same API call in the order specified,
                though some layers may be interleaved together using
                hardware-specific batching.
            program_id: A user-provided identifier for the program. This must be
                unique within the Google Cloud project being used. If this
                parameter is not provided, a random id of the format
                'calibration-################YYMMDD' will be generated,
                where # is alphanumeric and YYMMDD is the current year, month,
                and day.
            gate_set: The gate set used to serialize the circuits in each
                layer.  The gate set must be supported by the processor.
            description: An optional description to set on the program.
            labels: Optional set of labels to set on the program.

        Returns:
            A EngineProgram for the newly created program.
        """
        if not gate_set:
            raise ValueError('Gate set must be specified.')
        if not program_id:
            program_id = _make_random_id('calibration-')

        calibration = v2.calibration_pb2.FocusedCalibration()
        for layer in layers:
            new_layer = calibration.layers.add()
            new_layer.calibration_type = layer.calibration_type
            for arg in layer.args:
                arg_to_proto(layer.args[arg], out=new_layer.args[arg])
            gate_set.serialize(layer.program, msg=new_layer.layer)

        new_program_id, new_program = self.context.client.create_program(
            self.project_id,
            program_id,
            code=self._pack_any(calibration),
            description=description,
            labels=labels,
        )

        return engine_program.EngineProgram(
            self.project_id,
            new_program_id,
            self.context,
            new_program,
            result_type=ResultType.Calibration,
        )

    def _serialize_program(self, program: cirq.Circuit, gate_set: Serializer) -> any_pb2.Any:
        if not isinstance(program, cirq.Circuit):
            raise TypeError(f'Unrecognized program type: {type(program)}')
        program.device.validate_circuit(program)

        if self.context.proto_version == ProtoVersion.V2:
            program = gate_set.serialize(program)
            return self._pack_any(program)
        else:
            raise ValueError(f'invalid program proto version: {self.context.proto_version}')

    def _pack_any(self, message: 'google.protobuf.Message') -> any_pb2.Any:
        """Packs a message into an Any proto.

        Returns the packed Any proto.
        """
        packed = any_pb2.Any()
        packed.Pack(message)
        return packed

    def get_program(self, program_id: str) -> engine_program.EngineProgram:
        """Returns an EngineProgram for an existing Quantum Engine program.

        Args:
            program_id: Unique ID of the program within the parent project.

        Returns:
            A EngineProgram for the program.
        """
        return engine_program.EngineProgram(self.project_id, program_id, self.context)

    def list_programs(
        self,
        created_before: Optional[Union[datetime.datetime, datetime.date]] = None,
        created_after: Optional[Union[datetime.datetime, datetime.date]] = None,
        has_labels: Optional[Dict[str, str]] = None,
    ) -> List[engine_program.EngineProgram]:
        """Returns a list of previously executed quantum programs.

        Args:
            created_after: retrieve programs that were created after this date
                or time.
            created_before: retrieve programs that were created after this date
                or time.
            has_labels: retrieve programs that have labels on them specified by
                this dict. If the value is set to `*`, filters having the label
                regardless of the label value will be filtered. For example, to
                query programs that have the shape label and have the color
                label with value red can be queried using
                `{'color: red', 'shape:*'}`
        """

        client = self.context.client
        response = client.list_programs(
            self.project_id,
            created_before=created_before,
            created_after=created_after,
            has_labels=has_labels,
        )
        return [
            engine_program.EngineProgram(
                project_id=engine_client._ids_from_program_name(p.name)[0],
                program_id=engine_client._ids_from_program_name(p.name)[1],
                _program=p,
                context=self.context,
            )
            for p in response
        ]

    def list_jobs(
        self,
        created_before: Optional[Union[datetime.datetime, datetime.date]] = None,
        created_after: Optional[Union[datetime.datetime, datetime.date]] = None,
        has_labels: Optional[Dict[str, str]] = None,
        execution_states: Optional[Set[quantum.enums.ExecutionStatus.State]] = None,
    ):
        """Returns the list of jobs in the project.

        All historical jobs can be retrieved using this method and filtering
        options are available too, to narrow down the search baesd on:
          * creation time
          * job labels
          * execution states

        Args:
            created_after: retrieve jobs that were created after this date
                or time.
            created_before: retrieve jobs that were created after this date
                or time.
            has_labels: retrieve jobs that have labels on them specified by
                this dict. If the value is set to `*`, filters having the label
                regardless of the label value will be filtered. For example, to
                query programs that have the shape label and have the color
                label with value red can be queried using

                {'color': 'red', 'shape':'*'}

            execution_states: retrieve jobs that have an execution state  that
                 is contained in `execution_states`. See
                 `quantum.enums.ExecutionStatus.State` enum for accepted values.
        """
        client = self.context.client
        response = client.list_jobs(
            self.project_id,
            None,
            created_before=created_before,
            created_after=created_after,
            has_labels=has_labels,
            execution_states=execution_states,
        )
        return [
            engine_job.EngineJob(
                project_id=engine_client._ids_from_job_name(j.name)[0],
                program_id=engine_client._ids_from_job_name(j.name)[1],
                job_id=engine_client._ids_from_job_name(j.name)[2],
                context=self.context,
                _job=j,
            )
            for j in response
        ]

    def list_processors(self) -> List[engine_processor.EngineProcessor]:
        """Returns a list of Processors that the user has visibility to in the
        current Engine project. The names of these processors are used to
        identify devices when scheduling jobs and gathering calibration metrics.

        Returns:
            A list of EngineProcessors to access status, device and calibration
            information.
        """
        response = self.context.client.list_processors(self.project_id)
        return [
            engine_processor.EngineProcessor(
                self.project_id,
                engine_client._ids_from_processor_name(p.name)[1],
                self.context,
                p,
            )
            for p in response
        ]

    def get_processor(self, processor_id: str) -> engine_processor.EngineProcessor:
        """Returns an EngineProcessor for a Quantum Engine processor.

        Args:
            processor_id: The processor unique identifier.

        Returns:
            A EngineProcessor for the processor.
        """
        return engine_processor.EngineProcessor(self.project_id, processor_id, self.context)

    def sampler(
        self, processor_id: Union[str, List[str]], gate_set: Serializer
    ) -> engine_sampler.QuantumEngineSampler:
        """Returns a sampler backed by the engine.

        Args:
            processor_id: String identifier, or list of string identifiers,
                determining which processors may be used when sampling.
            gate_set: Determines how to serialize circuits when requesting
                samples.
        """
        return engine_sampler.QuantumEngineSampler(
            engine=self, processor_id=processor_id, gate_set=gate_set
        )


def get_engine(project_id: Optional[str] = None) -> Engine:
    """Get an Engine instance assuming some sensible defaults.

    This uses the environment variable GOOGLE_CLOUD_PROJECT for the Engine
    project_id, unless set explicitly. By using an environment variable,
    you can avoid hard-coding the project_id in shared code.

    If the environment variables are set, but incorrect, an authentication
    failure will occur when attempting to run jobs on the engine.

    Args:
        project_id: If set overrides the project id obtained from the
            environment variable `GOOGLE_CLOUD_PROJECT`.

    Returns:
        The Engine instance.

    Raises:
        EnvironmentError: If the environment variable GOOGLE_CLOUD_PROJECT is
            not set.
    """
    env_project_id = 'GOOGLE_CLOUD_PROJECT'
    if not project_id:
        project_id = os.environ.get(env_project_id)
    if not project_id:
        raise EnvironmentError(f'Environment variable {env_project_id} is not set.')

    return Engine(project_id=project_id)


def get_engine_device(
    processor_id: str,
    project_id: Optional[str] = None,
    gatesets: Iterable[SerializableGateSet] = (),
) -> cirq.Device:
    """Returns a `Device` object for a given processor.

    This is a short-cut for creating an engine object, getting the
    processor object, and retrieving the device.  Note that the
    gateset is required in order to match the serialized specification
    back into cirq objects.
    """
    return get_engine(project_id).get_processor(processor_id).get_device(gatesets)


def get_engine_calibration(
    processor_id: str,
    project_id: Optional[str] = None,
) -> Optional['cirq_google.Calibration']:
    """Returns calibration metrics for a given processor.

    This is a short-cut for creating an engine object, getting the
    processor object, and retrieving the current calibration.
    May return None if no calibration metrics exist for the device.
    """
    return get_engine(project_id).get_processor(processor_id).get_current_calibration()
