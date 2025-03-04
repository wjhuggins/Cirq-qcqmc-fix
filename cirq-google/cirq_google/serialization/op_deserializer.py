# Copyright 2019 The Cirq Developers
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

from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
)
from dataclasses import dataclass

import abc
import sympy

import cirq
from cirq._compat import deprecated
from cirq_google.api import v2
from cirq_google.ops.calibration_tag import CalibrationTag
from cirq_google.serialization import arg_func_langs


class OpDeserializer(abc.ABC):
    """Generic supertype for operation deserializers.

    Each operation deserializer describes how to deserialize operation protos
    with a particular `serialized_id` to a specific type of Cirq operation.
    """

    @property
    @abc.abstractmethod
    def serialized_id(self) -> str:
        """Returns the string identifier for the accepted serialized objects.

        This ID denotes the serialization format this deserializer consumes. For
        example, one of the common deserializers converts objects with the id
        'xy' into PhasedXPowGates.
        """

    @abc.abstractmethod
    def from_proto(
        self,
        proto,
        *,
        arg_function_language: str = '',
        constants: List[v2.program_pb2.Constant] = None,
        deserialized_constants: List[Any] = None,
    ) -> cirq.Operation:
        """Converts a proto-formatted operation into a Cirq operation.

        Args:
            proto: The proto object to be deserialized.
            arg_function_language: The `arg_function_language` field from
                `Program.Language`.
            constants: The list of Constant protos referenced by constant
                table indices in `proto`.
            deserialized_constants: The deserialized contents of `constants`.

        Returns:
            The deserialized operation represented by `proto`.
        """


@dataclass(frozen=True)
class DeserializingArg:
    """Specification of the arguments to deserialize an argument to a gate.

    Args:
        serialized_name: The serialized name of the gate that is being
            deserialized.
        constructor_arg_name: The name of the argument in the constructor of
            the gate corresponding to this serialized argument.
        value_func: Sometimes a value from the serialized proto needs to
            converted to an appropriate type or form. This function takes the
            serialized value and returns the appropriate type. Defaults to
            None.
        required: Whether a value must be specified when constructing the
            deserialized gate. Defaults to True.
        default: default value to set if the value is not present in the
            arg.  If set, required is ignored.
    """

    serialized_name: str
    constructor_arg_name: str
    value_func: Optional[Callable[[arg_func_langs.ARG_LIKE], Any]] = None
    required: bool = True
    default: Any = None


class GateOpDeserializer(OpDeserializer):
    """Describes how to deserialize a proto to a given Gate type.

    Attributes:
        serialized_gate_id: The id used when serializing the gate.
    """

    def __init__(
        self,
        serialized_gate_id: str,
        gate_constructor: Callable,
        args: Sequence[DeserializingArg],
        num_qubits_param: Optional[str] = None,
        op_wrapper: Callable[
            [cirq.Operation, v2.program_pb2.Operation], cirq.Operation
        ] = lambda x, y: x,
        deserialize_tokens: Optional[bool] = True,
    ):
        """Constructs a deserializer.

        Args:
            serialized_gate_id: The serialized id of the gate that is being
                deserialized.
            gate_constructor: A function that produces the deserialized gate
                given arguments from args.
            args: A list of the arguments to be read from the serialized
                gate and the information required to use this to construct
                the gate using the gate_constructor above.
            num_qubits_param: Some gate constructors require that the number
                of qubits be passed to their constructor. This is the name
                of the parameter in the constructor for this value. If None,
                no number of qubits is passed to the constructor.
            op_wrapper: An optional Callable to modify the resulting
                GateOperation, for instance, to add tags
            deserialize_tokens: Whether to convert tokens to
                CalibrationTags. Defaults to True.
        """
        self._serialized_gate_id = serialized_gate_id
        self._gate_constructor = gate_constructor
        self._args = args
        self._num_qubits_param = num_qubits_param
        self._op_wrapper = op_wrapper
        self._deserialize_tokens = deserialize_tokens

    @property  # type: ignore
    @deprecated(deadline='v0.13', fix='Use serialized_id instead.')
    def serialized_gate_id(self) -> str:
        return self.serialized_id

    @property
    def serialized_id(self):
        return self._serialized_gate_id

    def from_proto(
        self,
        proto: v2.program_pb2.Operation,
        *,
        arg_function_language: str = '',
        constants: List[v2.program_pb2.Constant] = None,
        deserialized_constants: List[Any] = None,  # unused
    ) -> cirq.Operation:
        """Turns a cirq_google.api.v2.Operation proto into a GateOperation.

        Args:
            proto: The proto object to be deserialized.
            arg_function_language: The `arg_function_language` field from
                `Program.Language`.
            constants: The list of Constant protos referenced by constant
                table indices in `proto`.
            deserialized_constants: Unused in this method.

        Returns:
            The deserialized GateOperation represented by `proto`.
        """
        qubits = [v2.qubit_from_proto_id(q.id) for q in proto.qubits]
        args = self._args_from_proto(proto, arg_function_language=arg_function_language)
        if self._num_qubits_param is not None:
            args[self._num_qubits_param] = len(qubits)
        gate = self._gate_constructor(**args)
        op = self._op_wrapper(gate.on(*qubits), proto)
        if self._deserialize_tokens:
            which = proto.WhichOneof('token')
            if which == 'token_constant_index':
                if not constants:
                    raise ValueError(
                        'Proto has references to constants table '
                        'but none was passed in, value ='
                        f'{proto}'
                    )
                op = op.with_tags(
                    CalibrationTag(constants[proto.token_constant_index].string_value)
                )
            elif which == 'token_value':
                op = op.with_tags(CalibrationTag(proto.token_value))
        return op

    def _args_from_proto(
        self, proto: v2.program_pb2.Operation, *, arg_function_language: str
    ) -> Dict[str, arg_func_langs.ARG_LIKE]:
        return_args = {}
        for arg in self._args:
            if arg.serialized_name not in proto.args:
                if arg.default:
                    return_args[arg.constructor_arg_name] = arg.default
                    continue
                elif arg.required:
                    raise ValueError(
                        f'Argument {arg.serialized_name} '
                        'not in deserializing args, but is required.'
                    )

            value = arg_func_langs.arg_from_proto(
                proto.args[arg.serialized_name],
                arg_function_language=arg_function_language,
                required_arg_name=None if not arg.required else arg.serialized_name,
            )

            if arg.value_func is not None:
                value = arg.value_func(value)

            if value is not None:
                return_args[arg.constructor_arg_name] = value
        return return_args


class CircuitOpDeserializer(OpDeserializer):
    """Describes how to serialize CircuitOperations."""

    @property
    def serialized_id(self):
        return 'circuit'

    def from_proto(
        self,
        proto: v2.program_pb2.CircuitOperation,
        *,
        arg_function_language: str = '',
        constants: List[v2.program_pb2.Constant] = None,
        deserialized_constants: List[Any] = None,
    ) -> cirq.CircuitOperation:
        """Turns a cirq.google.api.v2.CircuitOperation proto into a CircuitOperation.

        Args:
            proto: The proto object to be deserialized.
            arg_function_language: The `arg_function_language` field from
                `Program.Language`.
            constants: The list of Constant protos referenced by constant
                table indices in `proto`. This list should already have been
                parsed to produce 'deserialized_constants'.
            deserialized_constants: The deserialized contents of `constants`.

        Returns:
            The deserialized CircuitOperation represented by `proto`.
        """
        if constants is None or deserialized_constants is None:
            raise ValueError(
                'CircuitOp deserialization requires a constants list and a corresponding list of '
                'post-deserialization values (deserialized_constants).'
            )
        if len(deserialized_constants) <= proto.circuit_constant_index:
            raise ValueError(
                f'Constant index {proto.circuit_constant_index} in CircuitOperation '
                'does not appear in the deserialized_constants list '
                f'(length {len(deserialized_constants)}).'
            )
        circuit = deserialized_constants[proto.circuit_constant_index]
        if not isinstance(circuit, cirq.FrozenCircuit):
            raise ValueError(
                f'Constant at index {proto.circuit_constant_index} was expected to be a circuit, '
                f'but it has type {type(circuit)} in the deserialized_constants list.'
            )

        which_rep_spec = proto.repetition_specification.WhichOneof('repetition_value')
        if which_rep_spec == 'repetition_count':
            rep_ids = None
            repetitions = proto.repetition_specification.repetition_count
        elif which_rep_spec == 'repetition_ids':
            rep_ids = proto.repetition_specification.repetition_ids.ids
            repetitions = len(rep_ids)
        else:
            rep_ids = None
            repetitions = 1

        qubit_map = {
            v2.qubit_from_proto_id(entry.key.id): v2.qubit_from_proto_id(entry.value.id)
            for entry in proto.qubit_map.entries
        }
        measurement_key_map = {
            entry.key.string_key: entry.value.string_key
            for entry in proto.measurement_key_map.entries
        }
        arg_map = {
            arg_func_langs.arg_from_proto(
                entry.key, arg_function_language=arg_function_language
            ): arg_func_langs.arg_from_proto(
                entry.value, arg_function_language=arg_function_language
            )
            for entry in proto.arg_map.entries
        }

        for arg in arg_map.keys():
            if not isinstance(arg, (str, sympy.Symbol)):
                raise ValueError(
                    'Invalid key parameter type in deserialized CircuitOperation. '
                    f'Expected str or sympy.Symbol, found {type(arg)}.'
                    f'\nFull arg: {arg}'
                )

        for arg in arg_map.values():
            if not isinstance(arg, (str, sympy.Symbol, float, int)):
                raise ValueError(
                    'Invalid value parameter type in deserialized CircuitOperation. '
                    f'Expected str, sympy.Symbol, or number; found {type(arg)}.'
                    f'\nFull arg: {arg}'
                )

        return cirq.CircuitOperation(
            circuit,
            repetitions,
            qubit_map,
            measurement_key_map,
            arg_map,  # type: ignore
            rep_ids,
        )
