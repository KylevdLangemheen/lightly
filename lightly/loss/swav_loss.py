from typing import List, Union

import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor


@torch.no_grad()
def sinkhorn(
    out: Tensor,
    iterations: int = 3,
    epsilon: float = 0.05,
    gather_distributed: bool = False,
) -> Tensor:
    """Distributed sinkhorn algorithm.

    As outlined in [0] and implemented in [1].

    - [0]: SwaV, 2020, https://arxiv.org/abs/2006.09882
    - [1]: https://github.com/facebookresearch/swav/

    Args:
        out:
            Similarity of the features and the SwaV prototypes.
        iterations:
            Number of sinkhorn iterations.
        epsilon:
            Temperature parameter.
        gather_distributed:
            If True then features from all gpus are gathered to calculate the
            soft codes Q.

    Returns:
        Soft codes Q assigning each feature to a prototype.
    """
    world_size = 1
    if gather_distributed and dist.is_initialized():
        world_size = dist.get_world_size()

    # Get the exponential matrix and make it sum to 1
    Q = torch.exp(out / epsilon).t()
    sum_Q = torch.sum(Q)
    if world_size > 1:
        dist.all_reduce(sum_Q)
    Q /= sum_Q

    B = Q.shape[1] * world_size

    for _ in range(iterations):
        # Normalize rows
        sum_of_rows = torch.sum(Q, dim=1, keepdim=True)
        if world_size > 1:
            dist.all_reduce(sum_of_rows)
        Q /= sum_of_rows
        # Normalize columns
        Q /= torch.sum(Q, dim=0, keepdim=True)
        Q /= B

    Q *= B
    return Q.t()


class SwaVLoss(nn.Module):
    """Implementation of the SwaV loss.

    Attributes:
        temperature:
            Temperature parameter used for cross entropy calculations.
        sinkhorn_iterations:
            Number of iterations of the sinkhorn algorithm.
        sinkhorn_epsilon:
            Temperature parameter used in the sinkhorn algorithm.
        sinkhorn_gather_distributed:
            If True, features from all GPUs are gathered to calculate the
            soft codes in the sinkhorn algorithm.
    """

    def __init__(
        self,
        temperature: float = 0.1,
        sinkhorn_iterations: int = 3,
        sinkhorn_epsilon: float = 0.05,
        sinkhorn_gather_distributed: bool = False,
    ):
        """Initializes the SwaVLoss module with the specified parameters.

        Args:
            temperature:
                Temperature parameter used for cross-entropy calculations.
            sinkhorn_iterations:
                Number of iterations of the sinkhorn algorithm.
            sinkhorn_epsilon:
                Temperature parameter used in the sinkhorn algorithm.
            sinkhorn_gather_distributed:
                If True, features from all GPUs are gathered to calculate the
                soft codes in the sinkhorn algorithm.

        Raises:
            ValueError: If sinkhorn_gather_distributed is True but torch.distributed
                is not available.
        """
        super(SwaVLoss, self).__init__()
        if sinkhorn_gather_distributed and not dist.is_available():
            raise ValueError(
                "sinkhorn_gather_distributed is True but torch.distributed is not "
                "available. Please set gather_distributed=False or install a torch "
                "version with distributed support."
            )

        self.temperature = temperature
        self.sinkhorn_iterations = sinkhorn_iterations
        self.sinkhorn_epsilon = sinkhorn_epsilon
        self.sinkhorn_gather_distributed = sinkhorn_gather_distributed

    def subloss(self, z: Tensor, q: Tensor) -> Tensor:
        """Calculates the cross entropy for the SwaV prediction problem.

        Args:
            z:
                Similarity of the features and the SwaV prototypes.
            q:
                Codes obtained from Sinkhorn iterations.

        Returns:
            Cross entropy between predictions z and codes q.
        """
        return -torch.mean(
            torch.sum(q * F.log_softmax(z / self.temperature, dim=1), dim=1)
        )

    def forward(
        self,
        high_resolution_outputs: List[Tensor],
        low_resolution_outputs: List[Tensor],
        queue_outputs: Union[List[Tensor], None] = None,
    ) -> Tensor:
        """Computes the SwaV loss for a set of high and low resolution outputs.

        - [0]: SwaV, 2020, https://arxiv.org/abs/2006.09882

        Args:
            high_resolution_outputs:
                List of similarities of features and SwaV prototypes for the
                high resolution crops.
            low_resolution_outputs:
                List of similarities of features and SwaV prototypes for the
                low resolution crops.
            queue_outputs:
                List of similarities of features and SwaV prototypes for the
                queue of high resolution crops from previous batches.

        Returns:
            Swapping assignments between views loss (SwaV) as described in [0].
        """
        n_crops = len(high_resolution_outputs) + len(low_resolution_outputs)

        # Multi-crop iterations
        loss = high_resolution_outputs[0].new_zeros(1)
        for i in range(len(high_resolution_outputs)):
            # Compute codes of i-th high resolution crop
            with torch.no_grad():
                outputs = high_resolution_outputs[i].detach()

                # Append queue outputs
                if queue_outputs is not None:
                    outputs = torch.cat((outputs, queue_outputs[i].detach()))

                # Compute the codes
                q = sinkhorn(
                    outputs,
                    iterations=self.sinkhorn_iterations,
                    epsilon=self.sinkhorn_epsilon,
                    gather_distributed=self.sinkhorn_gather_distributed,
                )

                # Drop queue similarities
                if queue_outputs is not None:
                    q = q[: len(high_resolution_outputs[i])]

            # Compute subloss for each pair of crops
            subloss = high_resolution_outputs[i].new_zeros(1)
            for v in range(len(high_resolution_outputs)):
                if v != i:
                    subloss += self.subloss(high_resolution_outputs[v], q)

            for v in range(len(low_resolution_outputs)):
                subloss += self.subloss(low_resolution_outputs[v], q)

            loss += subloss / (n_crops - 1)

        return loss / len(high_resolution_outputs)
