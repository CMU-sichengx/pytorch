import itertools
from typing import List, Set, Tuple

import torch
import torch.distributed as dist
import torch.nn as nn

from torch.distributed._tensor import DeviceMesh, DTensor, init_device_mesh
from torch.distributed.device_mesh import _get_device_handle
from ._fsdp_common import _is_composable_with_fsdp, FSDPMeshInfo
from ._fsdp_state import _get_module_fsdp_state


def _init_default_fully_shard_mesh() -> DeviceMesh:
    """Default to global CUDA mesh if possible else global CPU mesh."""
    if not dist.distributed_c10d.is_initialized():
        dist.distributed_c10d.init_process_group()
    default_pg = dist.distributed_c10d._get_default_group()
    device_type = "cuda" if torch.cuda.is_available() else "cpu"
    mesh = init_device_mesh(device_type, mesh_shape=(default_pg.size(),))
    return mesh


def _get_device_from_mesh(mesh: DeviceMesh) -> torch.device:
    if mesh.device_type == "cpu":
        return torch.device("cpu")
    device_handle = _get_device_handle(mesh.device_type)
    return torch.device(mesh.device_type, device_handle.current_device())


def _get_managed_modules(root_module: nn.Module) -> List[nn.Module]:
    modules: List[nn.Module] = []
    # Track visisted modules to avoid visiting shared modules multiple times
    visited_modules: Set[nn.Module] = set()

    def dfs(module: nn.Module) -> None:
        """
        Runs a DFS to collect managed modules, not recursing into modules with
        a non-composable API or ``fully_shard`` already applied.
        """
        if not _is_composable_with_fsdp(module):
            return
        elif module is not root_module and _get_module_fsdp_state(module) is not None:
            return  # nested `fully_shard` module
        visited_modules.add(module)
        for submodule in module.children():
            if submodule not in visited_modules:
                dfs(submodule)
        modules.append(module)

    dfs(root_module)
    return modules


def _get_managed_states(
    modules: List[nn.Module],
) -> Tuple[List[nn.Parameter], List[torch.Tensor]]:
    params: List[nn.Parameter] = []
    buffers: List[torch.Tensor] = []
    # Track visited parameters/buffers to avoid visiting shared parameters and
    # buffers multiple times
    visited_params: Set[nn.Parameter] = set()
    visited_buffers: Set[torch.Tensor] = set()
    for module in modules:
        for param in module.parameters(recurse=False):
            if param not in visited_params:
                params.append(param)
                visited_params.add(param)
        for buffer in module.buffers(recurse=False):
            if buffer not in visited_buffers:
                buffers.append(buffer)
                visited_buffers.add(buffer)
    return params, buffers


def _move_states_to_device(
    params: List[nn.Parameter],
    buffers: List[torch.Tensor],
    device: torch.device,
    mesh_info: FSDPMeshInfo,
) -> None:
    """
    We have FSDP move states to device for simpler and faster initialization
    since FSDP almost always uses CUDA for training. We move parameters/buffers
    rather than modules since modules to support ignoring parameters/buffers in
    the future.
    """
    # TODO: De-duplicate with `_apply` after `swap_tensors` path lands:
    # https://github.com/pytorch/pytorch/issues/115792
    for tensor in itertools.chain(params, buffers):
        if tensor.device == device:
            continue
        if isinstance(tensor, DTensor):
            if (dtensor_mesh_type := tensor._spec.mesh.device_type) != device.type:
                raise ValueError(
                    "Requires DTensor to have mesh of the same type as the FSDP mesh "
                    f"but got {dtensor_mesh_type} for DTensor and {device.type} for FSDP"
                )
            raise AssertionError(
                f"Expects DTensor to be moved to {dtensor_mesh_type} but got {tensor.device}"
            )
        tensor.data = tensor.to(device)
