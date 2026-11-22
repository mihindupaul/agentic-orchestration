from __future__ import annotations


class ControllerError(Exception):
    """Base class for controller errors."""


class DeviceError(ControllerError):
    """A device answered, and the answer was an error."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class DeviceTimeout(ControllerError):
    """A command was sent and no answer came back in time. The outcome is unknown."""


class ArmFault(ControllerError):
    """The arm is in an error or collision state. An operator must clear it."""


class OperationError(ControllerError):
    """An operation was asked for in a state where it cannot work, e.g. placing with an empty gripper."""


class GripperEmpty(OperationError):
    """The gripper closed on nothing: there was no vial to pick. Nothing is missing."""


class VialLost(OperationError):
    """A vial that was held is no longer in the gripper. Someone has to find it."""


class ControllerBusy(ControllerError):
    """Another operation is running. Nothing was done; ask again when it has finished."""


class ControllerFault(ControllerError):
    """The controller is faulted and refuses new work until the fault is cleared."""


class InvalidRequest(ControllerError):
    """The request is malformed or refers to things that do not exist (no such bay, well or operation)."""


ERRORS_BY_NAME = {c.__name__: c for c in (ControllerError, DeviceError, DeviceTimeout, ArmFault, ControllerBusy,
                                          ControllerFault, InvalidRequest, OperationError, GripperEmpty, VialLost)}
