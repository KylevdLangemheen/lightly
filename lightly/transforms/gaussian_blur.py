# Copyright (c) 2020. Lightly AG and its affiliates.
# All Rights Reserved

from typing import Optional, Tuple, Union
from warnings import warn

import numpy as np
from PIL import ImageFilter
from PIL.Image import Image
from torch import Tensor

from lightly.transforms.torchvision_v2_compatibility import functional as F


class GaussianBlur:
    """Implementation of random Gaussian blur.

    Utilizes the built-in ImageFilter method from PIL to apply a Gaussian
    blur to the input image with a certain probability. The blur is further
    randomized by sampling uniformly the values of the standard deviation of
    the Gaussian kernel.

    Attributes:
        kernel_size:
            Will be deprecated in favor of `sigmas` argument. If set, the old behavior applies and `sigmas` is ignored.
            Used to calculate sigma of gaussian blur with kernel_size * input_size.
        prob:
            Probability with which the blur is applied.
        scale:
            Will be deprecated in favor of `sigmas` argument. If set, the old behavior applies and `sigmas` is ignored.
            Used to scale the `kernel_size` of a factor of `kernel_scale`
        sigmas:
            Tuple of min and max value from which the std of the gaussian kernel is sampled.
            Is ignored if `kernel_size` is set.

    """

    def __init__(
        self,
        kernel_size: Optional[float] = None,
        prob: float = 0.5,
        scale: Optional[float] = None,
        sigmas: Tuple[float, float] = (0.2, 2),
    ):
        if scale != None or kernel_size != None:
            warn(
                "The 'kernel_size' and 'scale' arguments of the GaussianBlur augmentation will be deprecated.  "
                "Please use the 'sigmas' parameter instead.",
                DeprecationWarning,
            )
        self.prob = prob
        self.sigmas = sigmas

    def __call__(self, sample: Union[Tensor, Image]) -> Union[Tensor, Image]:
        """Blurs the image with a given probability.

        Args:
            sample:
                PIL image to which blur will be applied.

        Returns:
            Blurred image or original image.
        """
        prob = np.random.random_sample()

        # Convert to PIL image if it's a tensor, otherwise use as is
        is_input_tensor = isinstance(sample, Tensor)
        sample_pil: Image = F.to_pil_image(sample) if is_input_tensor else sample

        if prob < self.prob:
            # choose randomized std for Gaussian filtering
            sigma = np.random.uniform(self.sigmas[0], self.sigmas[1])
            # PIL GaussianBlur https://github.com/python-pillow/Pillow/blob/76478c6865c78af10bf48868345db2af92f86166/src/PIL/ImageFilter.py#L154 label the
            # sigma parameter of the gaussian filter as radius. Before, the radius of the patch was passed as the argument.
            # The issue was addressed here https://github.com/lightly-ai/lightly/issues/1051 and solved by AurelienGauffre.
            sample_pil = sample_pil.filter(ImageFilter.GaussianBlur(radius=sigma))

        # Convert back to tensor if input was a tensor
        return F.to_tensor(sample_pil) if is_input_tensor else sample_pil
