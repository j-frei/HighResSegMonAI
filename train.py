# Copyright 2020 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import shutil
import sys
import tempfile
from glob import glob

import nibabel as nib
import numpy as np
import torch
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

import monai
from monai.data import create_test_image_3d, list_data_collate
from monai.inferers import sliding_window_inference
from monai.metrics import DiceMetric
from monai.transforms import (
    AsChannelFirstd,
    AsChannelLastd,
    AddChanneld,
    Compose,
    LoadNiftid,
    RandCropByPosNegLabeld,
    RandRotated,
    RandZoomd,
    RandFlipd,
    RandShiftIntensityd,
    RandScaleIntensityd,
    RandAffined,
    RandGaussianNoised,
    ScaleIntensityd,
    ToTensord,
    DataStats,
    Resized,
)
from monai.visualize import plot_2d_or_3d_image


def main():
    monai.config.print_config()
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)

    # load dataset
    dpath = os.path.abspath(os.path.dirname(__file__))
    data = []

    bad_subjects = ["g085", "g087", "g098", "g109", "g147", "g192"]

    img_path_pattern = os.path.join(dpath, "data", "g*")
    for subject_base in sorted(glob(img_path_pattern)):
        if True in [ bad_subj in subject_base for bad_subj in bad_subjects ]:
            # subject_base path is one of the bad samples -> skip directory!
            continue
        imgL = os.path.join(subject_base, "volCentered_L.nii.gz")
        imgR = os.path.join(subject_base, "volCentered_R.nii.gz")
        segL = os.path.join(subject_base, "seg_L.nii.gz")
        segR = os.path.join(subject_base, "seg_R.nii.gz")
        data += [
            {"img": imgL, "seg": segL},
            {"img": imgR, "seg": segR}
        ]

    size_dataset = len(data)
    train_test_split = int(size_dataset*0.8)
    train_val_split = int(train_test_split*0.9)

    # 0 : train_val_split -> training
    # train_val_split : train_test_split -> validation
    # train_test_split : ... -> test
    data_train = data[:train_val_split]
    data_val = data[train_val_split:train_test_split]
    data_test = data[train_test_split:]


    # define transforms for image and segmentation
    train_transforms = Compose(
        [
            LoadNiftid(keys=["img", "seg"]),
            AsChannelFirstd(keys=["img", "seg"], channel_dim=-1),
            AddChanneld(keys=["img", "seg"]),
            Resized(keys=["img", "seg"], spatial_size=(80,120,160), mode="trilinear", align_corners=True),
            ScaleIntensityd(keys="img"),
            RandScaleIntensityd(keys="img", prob=0.4, factors=0.2),
            RandZoomd(keys=["img","seg"], prob=0.8, min_zoom=0.8, max_zoom=1.2),
            RandGaussianNoised(keys="img", prob=0.9),
            RandFlipd(keys=["img","seg"],prob=0.3, spatial_axis=[0,1,2]),
            ToTensord(keys=["img", "seg"]),
            RandAffined(keys=["img","seg"], prob=0.9, 
               translate_range=(15.,25.,35.),
               rotate_range=0.3,
               scale_range=0.15
            ),
        ]
    )
    val_transforms = Compose(
        [
            LoadNiftid(keys=["img", "seg"]),
            AsChannelFirstd(keys=["img", "seg"], channel_dim=-1),
            AddChanneld(keys=["img", "seg"]),
            Resized(keys=["img", "seg"], spatial_size=(80,120,160), mode="trilinear", align_corners=True),
            ScaleIntensityd(keys="img"),
            ToTensord(keys=["img", "seg"]),
        ]
    )

    # define dataset, data loader
    check_ds = monai.data.Dataset(data=data_train, transform=train_transforms)
    # use batch_size=2 to load images and use RandCropByPosNegLabeld to generate 2 x 4 images for network training
    check_loader = DataLoader(check_ds, batch_size=4, num_workers=4, collate_fn=list_data_collate)
    check_data = monai.utils.misc.first(check_loader)
    print(check_data["img"].shape, check_data["seg"].shape)

    # create a training data loader
    train_ds = monai.data.Dataset(data=data_train, transform=train_transforms)
    # use batch_size=2 to load images and use RandCropByPosNegLabeld to generate 2 x 4 images for network training
    train_loader = DataLoader(
        train_ds,
        batch_size=4,
        shuffle=True,
        num_workers=4,
        collate_fn=list_data_collate,
        pin_memory=torch.cuda.is_available(),
    )
    # create a validation data loader
    val_ds = monai.data.Dataset(data=data_val, transform=val_transforms)
    val_loader = DataLoader(val_ds, batch_size=1, num_workers=4, collate_fn=list_data_collate)
    dice_metric = DiceMetric(include_background=True, to_onehot_y=False, sigmoid=True, reduction="mean")

    # create UNet, DiceLoss and Adam optimizer
    device = torch.device("cuda:0")
    model = monai.networks.nets.UNet(
        dimensions=3,
        in_channels=1,
        out_channels=1,
        channels=(16, 32, 64, 128),
        strides=(2, 2, 2),
        num_res_units=2,
    ).to(device)
    loss_function = monai.losses.DiceLoss(sigmoid=True)
    optimizer = torch.optim.Adam(model.parameters(), 3e-4)

    # start a typical PyTorch training
    val_interval = 2
    best_metric = -1
    best_metric_epoch = -1
    epoch_loss_values = list()
    metric_values = list()
    writer = SummaryWriter()
    n_epochs = 4000
    for epoch in range(n_epochs):
        print("-" * 10)
        print(f"epoch {epoch + 1}/{n_epochs}")
        model.train()
        epoch_loss = 0
        step = 0
        for batch_data in train_loader:
            step += 1
            inputs, labels = batch_data["img"].to(device), batch_data["seg"].to(device)
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = loss_function(outputs, labels)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()
            epoch_len = len(train_ds) // train_loader.batch_size
            print(f"{step}/{epoch_len}, train_loss: {loss.item():.4f}")
            writer.add_scalar("train_loss", loss.item(), epoch_len * epoch + step)
        epoch_loss /= step
        epoch_loss_values.append(epoch_loss)
        print(f"epoch {epoch + 1} average loss: {epoch_loss:.4f}")

        if (epoch + 1) % val_interval == 0:
            model.eval()
            with torch.no_grad():
                metric_sum = 0.0
                metric_count = 0
                val_images = None
                val_labels = None
                val_outputs = None
                for val_data in val_loader:
                    val_images, val_labels = val_data["img"].to(device), val_data["seg"].to(device)
                    roi_size = (96, 96, 96)
                    sw_batch_size = 4
                    val_outputs = sliding_window_inference(val_images, roi_size, sw_batch_size, model)
                    value = dice_metric(y_pred=val_outputs, y=val_labels)
                    metric_count += len(value)
                    metric_sum += value.item() * len(value)
                metric = metric_sum / metric_count
                metric_values.append(metric)
                if metric > best_metric:
                    best_metric = metric
                    best_metric_epoch = epoch + 1
                    torch.save(model.state_dict(), "best_metric_model.pth")
                    print("saved new best metric model")
                print(
                    "current epoch: {} current mean dice: {:.4f} best mean dice: {:.4f} at epoch {}".format(
                        epoch + 1, metric, best_metric, best_metric_epoch
                    )
                )
                writer.add_scalar("val_mean_dice", metric, epoch + 1)
                # plot the last model output as GIF image in TensorBoard with the corresponding image and label
                plot_2d_or_3d_image(val_images, epoch + 1, writer, index=0, tag="image")
                plot_2d_or_3d_image(val_labels, epoch + 1, writer, index=0, tag="label")
                plot_2d_or_3d_image(val_outputs, epoch + 1, writer, index=0, tag="output")

    print(f"train completed, best_metric: {best_metric:.4f} at epoch: {best_metric_epoch}")
    writer.close()


if __name__ == "__main__":
    main()

