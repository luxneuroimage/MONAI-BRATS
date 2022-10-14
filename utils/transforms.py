import os
import shutil
import tempfile
import time
from typing import Any

import matplotlib.pyplot as plt
import monai.data
from monai.apps import DecathlonDataset
from monai.config import print_config
from monai.data import DataLoader, decollate_batch
from monai.deploy.operators.monai_seg_inference_operator import InMemImageReader
from monai.handlers.utils import from_engine
from monai.losses import DiceLoss
from monai.inferers import sliding_window_inference
from monai.metrics import DiceMetric
from monai.networks.nets import SegResNet
from monai.transforms import (
    Activations,
    Activationsd,
    AsDiscrete,
    AsDiscreted,
    Compose,
    Invertd,
    LoadImaged,
    MapTransform,
    NormalizeIntensityd,
    Orientationd,
    RandFlipd,
    RandScaleIntensityd,
    RandShiftIntensityd,
    RandSpatialCropd,
    Spacingd,
    EnsureTyped,
    EnsureChannelFirstd, EnsureChannelFirst, EnsureType, Orientation, Spacing, NormalizeIntensity, LoadImage, Transform,
    Lambda, AsChannelLast, SqueezeDim, SaveImage, SqueezeDimd, AsChannelLastd,
)
from monai.utils import set_determinism

import torch


class ConvertToMultiChannelBasedOnBratsClassesd(MapTransform):
    """
    Convert labels to multi channels based on brats classes:
    label 1 is the peritumoral edema
    label 2 is the GD-enhancing tumor
    label 3 is the necrotic and non-enhancing tumor core
    The possible classes are TC (Tumor core), WT (Whole tumor)
    and ET (Enhancing tumor).
    """
    def __call__(self, data):
        d = dict(data)
        for key in self.keys:
            result = []
            # merge label 2 and label 3 to construct TC
            result.append(torch.logical_or(d[key] == 2, d[key] == 3))
            # merge labels 1, 2 and 3 to construct WT
            result.append(
                torch.logical_or(
                    torch.logical_or(d[key] == 2, d[key] == 3), d[key] == 1
                )
            )
            # label 2 is ET
            result.append(d[key] == 2)
            d[key] = torch.stack(result, axis=0).float()
        return d

class ConvertToBratsClassesBasedOnMultiChannel(Transform):

    def __call__(self, data: Any):
        # order in data is: TC->WT->ET
        input_dict = {
            "TC": data[0],
            "WT": data[1],
            "ET": data[2]
        }

        output_dict = {}

        # ET is label 2
        output_dict["label2"] = input_dict["ET"]

        # label 3 is "TC and not label2" <=> "TC and not ET"
        output_dict["label3"] = torch.logical_or(input_dict["TC"], input_dict["ET"] != 1)

        # label 1 is "WT and not label 2 and not label 3"
        output_dict["label1"] = torch.logical_or(input_dict["WT"], torch.logical_or(output_dict["label2"] != 1, output_dict["label3"] != 1))

        # set values of different labels
        output_dict["label2"] = torch.mul(output_dict["label1"], 1)
        output_dict["label2"] = torch.mul(output_dict["label2"], 2)
        output_dict["label3"] = torch.mul(output_dict["label3"], 3)

        # merge the segmentations in one image
        output = torch.add(output_dict["label1"], torch.add(output_dict["label2"], output_dict["label3"]))
        assert(torch.any(output>3))
        data.set_array(output)
        new_data = monai.data.MetaTensor(output, meta=data.meta)
        return new_data


train_transform = Compose(
    [
        # load 4 Nifti images and stack them together,
        LoadImaged(keys=["image", "label"]),
        EnsureChannelFirstd(keys="image"),
        EnsureTyped(keys=["image", "label"]),
        ConvertToMultiChannelBasedOnBratsClassesd(keys="label"),
        Orientationd(keys=["image", "label"], axcodes="RAS"),
        Spacingd(
            keys=["image", "label"],
            pixdim=(1.0, 1.0, 1.0),
            mode=("bilinear", "nearest"),
        ),
        RandSpatialCropd(keys=["image", "label"], roi_size=[224, 224, 144], random_size=False),
        RandFlipd(keys=["image", "label"], prob=0.5, spatial_axis=0),
        RandFlipd(keys=["image", "label"], prob=0.5, spatial_axis=1),
        RandFlipd(keys=["image", "label"], prob=0.5, spatial_axis=2),
        NormalizeIntensityd(keys="image", nonzero=True, channel_wise=True),
        RandScaleIntensityd(keys="image", factors=0.1, prob=1.0),
        RandShiftIntensityd(keys="image", offsets=0.1, prob=1.0),
    ]
)

val_transform = Compose(
    [
        LoadImaged(keys=["image", "label"]),
        EnsureChannelFirstd(keys="image"),
        EnsureTyped(keys=["image", "label"]),
        ConvertToMultiChannelBasedOnBratsClassesd(keys="label"),
        Orientationd(keys=["image", "label"], axcodes="RAS"),
        Spacingd(
            keys=["image", "label"],
            pixdim=(1.0, 1.0, 1.0),
            mode=("bilinear", "nearest"),
        ),
        NormalizeIntensityd(keys="image", nonzero=True, channel_wise=True),
    ]
)

test_transform = Compose(
    [
        LoadImage(image_only=True),
        EnsureChannelFirst(),
        EnsureType(),
        Orientation(axcodes="RAS"),
        Spacing(
            pixdim=(1.0, 1.0, 1.0),
            mode=("bilinear"),
        ),
        NormalizeIntensity(nonzero=True, channel_wise=True),
    ]
)

post_trans = Compose(
    [
        Activations(sigmoid=True),
        AsDiscrete(threshold=0.5)
    ]
)

