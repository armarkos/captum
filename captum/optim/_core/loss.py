import functools
import operator
from abc import ABC, abstractmethod, abstractproperty
from typing import Any, Callable, Optional

import torch
import torch.nn as nn

from captum.optim._utils.image.common import get_neuron_pos
from captum.optim._utils.typing import ModuleOutputMapping


def _make_arg_str(arg):
    arg = str(arg)
    too_big = len(arg) > 15 or "\n" in arg
    return arg[:15] + "..." if too_big else arg


class Loss(ABC):
    """
    Abstract Class to describe loss.
    Note: All Loss classes should expose self.target for hooking by
    InputOptimization
    """

    def __init__(self) -> None:
        super(Loss, self).__init__()

    @abstractproperty
    def target(self):
        pass

    @abstractmethod
    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        pass

    def __repr__(self):
        return self.__name__

    def __neg__(self):
        return module_op(self, None, operator.neg)

    def __add__(self, other):
        return module_op(self, other, operator.add)

    def __sub__(self, other):
        return module_op(self, other, operator.sub)

    def __mul__(self, other):
        return module_op(self, other, operator.mul)

    def __truediv__(self, other):
        return module_op(self, other, operator.truediv)

    def __pow__(self, other):
        return module_op(self, other, operator.pow)

    def __radd__(self, other):
        return self.__add__(other)

    def __rsub__(self, other):
        return self.__neg__().__add__(other)

    def __rmul__(self, other):
        return self.__mul__(other)

    def __rtruediv__(self, other):
        if isinstance(other, (int, float)):

            def loss_fn(module):
                return operator.truediv(other, torch.mean(self(module)))

            name = self.__name__
            target = self.target
        elif isinstance(other, Loss):
            # This should never get called because __div__ will be called instead
            pass
        else:
            raise TypeError(
                "Can only apply math operations with int, float or Loss. Received type "
                + str(type(other))
            )
        return CompositeLoss(loss_fn, name=name, target=target)

    def __rpow__(self, other):
        if isinstance(other, (int, float)):

            def loss_fn(module):
                return operator.pow(other, torch.mean(self(module)))

            name = self.__name__
            target = self.target
        elif isinstance(other, Loss):
            # This should never get called because __pow__ will be called instead
            pass
        else:
            raise TypeError(
                "Can only apply math operations with int, float or Loss. Received type "
                + str(type(other))
            )
        return CompositeLoss(loss_fn, name=name, target=target)


def module_op(self: Loss, other: Any, math_op: Callable):
    """
    This is a general function for applying math operations to Losses
    """
    if other is None and math_op == operator.neg:

        def loss_fn(module):
            return math_op(self(module))

        name = self.__name__
        target = self.target
    elif isinstance(other, (int, float)):

        def loss_fn(module):
            return math_op(self(module), other)

        name = self.__name__
        target = self.target
    elif isinstance(other, Loss):
        # We take the mean of the output tensor to resolve shape mismatches
        def loss_fn(module):
            return math_op(torch.mean(self(module)), torch.mean(other(module)))

        name = f"Compose({', '.join([self.__name__, other.__name__])})"
        target = (
            self.target if hasattr(self.target, "__iter__") else [self.target]
        ) + (other.target if hasattr(other.target, "__iter__") else [other.target])
    else:
        raise TypeError(
            "Can only apply math operations with int, float or Loss. Received type "
            + str(type(other))
        )
    return CompositeLoss(loss_fn, name=name, target=target)


class SimpleLoss(Loss):
    def __init__(self, target: nn.Module = [], batch_index: Optional[int] = None):
        super(SimpleLoss, self).__init__()
        self._target = target
        self._batch_index = batch_index

    @property
    def target(self):
        return self._target

    @property
    def batch_index(self):
        return self._batch_index


class CompositeLoss(SimpleLoss):
    def __init__(self, loss_fn: Callable, name: str = "", target: nn.Module = []):
        super(CompositeLoss, self).__init__(target)
        self.__name__ = name
        self.loss_fn = loss_fn

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        return self.loss_fn(targets_to_values)


def loss_wrapper(cls):
    """
    Primarily for naming purposes.
    """

    @functools.wraps(cls)
    def wrapper(*args, **kwargs):
        obj = cls(*args, **kwargs)
        args_str = " [" + ", ".join([_make_arg_str(arg) for arg in args]) + "]"
        obj.__name__ = cls.__name__ + args_str
        return obj

    return wrapper


@loss_wrapper
class LayerActivation(SimpleLoss):
    """
    Maximize activations at the target layer.
    """

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations = targets_to_values[self.target]
        if self.batch_index is not None:
            activations = activations[self.batch_index]
        return activations


@loss_wrapper
class ChannelActivation(SimpleLoss):
    """
    Maximize activations at the target layer and target channel.
    """

    def __init__(
        self, target: nn.Module, channel_index: int, batch_index: Optional[int] = None
    ) -> None:
        SimpleLoss.__init__(self, target, batch_index)
        self.channel_index = channel_index

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations = targets_to_values[self.target]
        assert activations is not None
        # ensure channel_index is valid
        assert self.channel_index < activations.shape[1]
        # assume NCHW
        # NOTE: not necessarily true e.g. for Linear layers
        # assert len(activations.shape) == 4
        if self.batch_index is None:
            return activations[:, self.channel_index, ...]
        else:
            return activations[self.batch_index, self.channel_index, ...]


@loss_wrapper
class NeuronActivation(SimpleLoss):
    def __init__(
        self,
        target: nn.Module,
        channel_index: int,
        x: Optional[int] = None,
        y: Optional[int] = None,
        batch_index: Optional[int] = None,
    ) -> None:
        SimpleLoss.__init__(self, target, batch_index)
        self.channel_index = channel_index
        self.x = x
        self.y = y

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations = targets_to_values[self.target]
        assert activations is not None
        assert self.channel_index < activations.shape[1]
        assert len(activations.shape) == 4  # assume NCHW
        _x, _y = get_neuron_pos(
            activations.size(2), activations.size(3), self.x, self.y
        )

        if self.batch_index is None:
            return activations[:, self.channel_index, _x : _x + 1, _y : _y + 1]
        else:
            return activations[
                self.batch_index, self.channel_index, _x : _x + 1, _y : _y + 1
            ]


@loss_wrapper
class DeepDream(SimpleLoss):
    """
    Maximize 'interestingness' at the target layer.
    Mordvintsev et al., 2015.
    """

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations = targets_to_values[self.target]
        if self.batch_index is not None:
            activations = activations[self.batch_index]
        return activations ** 2


@loss_wrapper
class TotalVariation(SimpleLoss):
    """
    Total variation denoising penalty for activations.
    See Mahendran, V. 2014. Understanding Deep Image Representations by Inverting Them.
    https://arxiv.org/abs/1412.0035
    """

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations = targets_to_values[self.target]
        if self.batch_index is not None:
            activations = activations[self.batch_index]
        x_diff = activations[..., 1:, :] - activations[..., :-1, :]
        y_diff = activations[..., :, 1:] - activations[..., :, :-1]
        return torch.sum(torch.abs(x_diff)) + torch.sum(torch.abs(y_diff))


@loss_wrapper
class L1(SimpleLoss):
    """
    L1 norm of the target layer, generally used as a penalty.
    """

    def __init__(
        self,
        target: nn.Module,
        constant: float = 0.0,
        batch_index: Optional[int] = None,
    ) -> None:
        SimpleLoss.__init__(self, target, batch_index)
        self.constant = constant

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations = targets_to_values[self.target]
        if self.batch_index is not None:
            activations = activations[self.batch_index]
        return torch.abs(activations - self.constant).sum()


@loss_wrapper
class L2(SimpleLoss):
    """
    L2 norm of the target layer, generally used as a penalty.
    """

    def __init__(
        self,
        target: nn.Module,
        constant: float = 0.0,
        epsilon: float = 1e-6,
        batch_index: Optional[int] = None,
    ) -> None:
        SimpleLoss.__init__(self, target, batch_index)
        self.constant = constant
        self.epsilon = epsilon

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations = targets_to_values[self.target]
        activations = ((activations - self.constant) ** 2).sum()
        if self.batch_index is not None:
            activations = activations[self.batch_index]
        return torch.sqrt(self.epsilon + activations)


@loss_wrapper
class Diversity(SimpleLoss):
    """
    Use a cosine similarity penalty to extract features from a polysemantic neuron.
    Olah, Mordvintsev & Schubert, 2017.
    https://distill.pub/2017/feature-visualization/#diversity
    """

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations = targets_to_values[self.target]
        return -sum(
            [
                sum(
                    [
                        (
                            torch.cosine_similarity(
                                activations[j].view(1, -1), activations[i].view(1, -1)
                            )
                        ).sum()
                        for i in range(activations.size(0))
                        if i != j
                    ]
                )
                for j in range(activations.size(0))
            ]
        ) / activations.size(0)


@loss_wrapper
class ActivationInterpolation(SimpleLoss):
    """
    Interpolate between two different layers & channels.
    Olah, Mordvintsev & Schubert, 2017.
    https://distill.pub/2017/feature-visualization/#Interaction-between-Neurons
    """

    def __init__(
        self,
        target1: nn.Module = None,
        channel_index1: int = -1,
        target2: nn.Module = None,
        channel_index2: int = -1,
    ) -> None:
        self.target_one = target1
        self.channel_index_one = channel_index1
        self.target_two = target2
        self.channel_index_two = channel_index2
        # Expose targets for InputOptimization
        SimpleLoss.__init__(self, [target1, target2])

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations_one = targets_to_values[self.target_one]
        activations_two = targets_to_values[self.target_two]

        assert activations_one is not None and activations_two is not None
        # ensure channel indices are valid
        assert (
            self.channel_index_one < activations_one.shape[1]
            and self.channel_index_two < activations_two.shape[1]
        )
        assert activations_one.size(0) == activations_two.size(0)

        if self.channel_index_one > -1:
            activations_one = activations_one[:, self.channel_index_one]
        if self.channel_index_two > -1:
            activations_two = activations_two[:, self.channel_index_two]
        B = activations_one.size(0)

        batch_weights = torch.arange(B, device=activations_one.device) / (B - 1)
        sum_tensor = torch.zeros(1, device=activations_one.device)
        for n in range(B):
            sum_tensor = (
                sum_tensor + ((1 - batch_weights[n]) * activations_one[n]).mean()
            )
            sum_tensor = sum_tensor + (batch_weights[n] * activations_two[n]).mean()
        return sum_tensor


@loss_wrapper
class Alignment(SimpleLoss):
    """
    Penalize the L2 distance between tensors in the batch to encourage visual
    similarity between them.
    Olah, Mordvintsev & Schubert, 2017.
    https://distill.pub/2017/feature-visualization/#Interaction-between-Neurons
    """

    def __init__(self, target: nn.Module, decay_ratio: float = 2.0) -> None:
        SimpleLoss.__init__(self, target)
        self.decay_ratio = decay_ratio

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations = targets_to_values[self.target]
        B = activations.size(0)

        sum_tensor = torch.zeros(1, device=activations.device)
        for d in [1, 2, 3, 4]:
            for i in range(B - d):
                a, b = i, i + d
                activ_a, activ_b = activations[a], activations[b]
                sum_tensor = sum_tensor + (
                    (activ_a - activ_b) ** 2
                ).mean() / self.decay_ratio ** float(d)

        return sum_tensor


@loss_wrapper
class Direction(SimpleLoss):
    """
    Visualize a general direction vector.
    Carter, et al., "Activation Atlas", Distill, 2019.
    https://distill.pub/2019/activation-atlas/#Aggregating-Multiple-Images
    """

    def __init__(
        self,
        target: nn.Module,
        vec: torch.Tensor,
        batch_index: Optional[int] = None,
    ) -> None:
        SimpleLoss.__init__(self, target, batch_index)
        self.direction = vec.reshape((1, -1, 1, 1))

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations = targets_to_values[self.target]
        assert activations.size(1) == self.direction.size(1)
        if self.batch_index is not None:
            activations = activations[self.batch_index]
        return torch.cosine_similarity(self.direction, activations)


@loss_wrapper
class NeuronDirection(SimpleLoss):
    """
    Visualize a single (x, y) position for a direction vector.
    Carter, et al., "Activation Atlas", Distill, 2019.
    https://distill.pub/2019/activation-atlas/#Aggregating-Multiple-Images
    """

    def __init__(
        self,
        target: nn.Module,
        vec: torch.Tensor,
        x: Optional[int] = None,
        y: Optional[int] = None,
        channel_index: Optional[int] = None,
        batch_index: Optional[int] = None,
    ) -> None:
        SimpleLoss.__init__(self, target, batch_index)
        self.direction = vec.reshape((1, -1, 1, 1))
        self.x = x
        self.y = y
        self.channel_index = channel_index

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations = targets_to_values[self.target]

        assert activations.dim() == 4

        _x, _y = get_neuron_pos(
            activations.size(2), activations.size(3), self.x, self.y
        )
        activations = activations[:, :, _x : _x + 1, _y : _y + 1]
        if self.channel_index is not None:
            activations = activations[:, self.channel_index, ...][:, None, ...]
        if self.batch_index is not None:
            activations = activations[self.batch_index]
        return torch.cosine_similarity(self.direction, activations)


@loss_wrapper
class TensorDirection(SimpleLoss):
    """
    Visualize a tensor direction vector.
    Carter, et al., "Activation Atlas", Distill, 2019.
    https://distill.pub/2019/activation-atlas/#Aggregating-Multiple-Images
    """

    def __init__(
        self, target: nn.Module, vec: torch.Tensor, batch_index: Optional[int] = None
    ) -> None:
        SimpleLoss.__init__(self, target, batch_index)
        self.direction = vec

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations = targets_to_values[self.target]

        assert activations.dim() == 4

        H_direction, W_direction = self.direction.size(2), self.direction.size(3)
        H_activ, W_activ = activations.size(2), activations.size(3)

        H = (H_activ - H_direction) // 2
        W = (W_activ - W_direction) // 2

        activations = activations[:, :, H : H + H_direction, W : W + W_direction]
        if self.batch_index is not None:
            activations = activations[self.batch_index]
        return torch.cosine_similarity(self.direction, activations)


@loss_wrapper
class ActivationWeights(SimpleLoss):
    """
    Apply weights to channels, neurons, or spots in the target.
    """

    def __init__(
        self,
        target: nn.Module,
        weights: torch.Tensor = None,
        neuron: bool = False,
        x: Optional[int] = None,
        y: Optional[int] = None,
        wx: Optional[int] = None,
        wy: Optional[int] = None,
    ) -> None:
        SimpleLoss.__init__(self, target)
        self.x = x
        self.y = y
        self.wx = wx
        self.wy = wy
        self.weights = weights
        self.neuron = x is not None or y is not None or neuron
        assert (
            wx is None
            and wy is None
            or wx is not None
            and wy is not None
            and x is not None
            and y is not None
        )

    def __call__(self, targets_to_values: ModuleOutputMapping) -> torch.Tensor:
        activations = targets_to_values[self.target]
        if self.neuron:
            assert activations.dim() == 4
            if self.wx is None and self.wy is None:
                _x, _y = get_neuron_pos(
                    activations.size(2), activations.size(3), self.x, self.y
                )
                activations = (
                    activations[..., _x : _x + 1, _y : _y + 1].squeeze() * self.weights
                )
            else:
                activations = activations[
                    ..., self.y : self.y + self.wy, self.x : self.x + self.wx
                ] * self.weights.view(1, -1, 1, 1)
        else:
            activations = activations * self.weights.view(1, -1, 1, 1)
        return activations
