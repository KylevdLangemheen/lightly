from pathlib import Path
from typing import Dict

from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import DeviceStatsMonitor, LearningRateMonitor
from pytorch_lightning.loggers import TensorBoardLogger
from torch.nn import Module
from torch.utils.data import DataLoader

from lightly.data import LightlyDataset
from lightly.transforms.torchvision_v2_compatibility import torchvision_transforms as T
from lightly.transforms.utils import IMAGENET_NORMALIZE
from lightly.utils.benchmarking import FinetuneClassifier, MetricCallback
from lightly.utils.dist import print_rank_zero


def finetune_eval(
    model: Module,
    train_dir: Path,
    val_dir: Path,
    log_dir: Path,
    batch_size_per_device: int,
    num_workers: int,
    accelerator: str,
    devices: int,
    precision: str,
    strategy: str,
    num_classes: int,
) -> Dict[str, float]:
    """Runs fine-tune evaluation on the given model.

    Parameters follow SimCLR [0] settings.

    The most important settings are:
        - Backbone: Frozen
        - Epochs: 30
        - Optimizer: SGD
        - Base Learning Rate: 0.05
        - Momentum: 0.9
        - Weight Decay: 0.0
        - LR Schedule: Cosine without warmup

    References:
        - [0]: SimCLR, 2020, https://arxiv.org/abs/2002.05709
    """
    print_rank_zero("Running fine-tune evaluation...")

    # Setup training data.
    train_transform = T.Compose(
        [
            T.RandomResizedCrop(224),
            T.RandomHorizontalFlip(),
            T.ToTensor(),
            T.Normalize(mean=IMAGENET_NORMALIZE["mean"], std=IMAGENET_NORMALIZE["std"]),
        ]
    )
    train_dataset = LightlyDataset(input_dir=str(train_dir), transform=train_transform)
    train_dataloader = DataLoader(
        train_dataset,
        batch_size=batch_size_per_device,
        shuffle=True,
        num_workers=num_workers,
        drop_last=True,
        persistent_workers=True,
    )

    # Setup validation data.
    val_transform = T.Compose(
        [
            T.Resize(256),
            T.CenterCrop(224),
            T.ToTensor(),
            T.Normalize(mean=IMAGENET_NORMALIZE["mean"], std=IMAGENET_NORMALIZE["std"]),
        ]
    )
    val_dataset = LightlyDataset(input_dir=str(val_dir), transform=val_transform)
    val_dataloader = DataLoader(
        val_dataset,
        batch_size=batch_size_per_device,
        shuffle=False,
        num_workers=num_workers,
        persistent_workers=True,
    )

    # Train linear classifier.
    metric_callback = MetricCallback()
    trainer = Trainer(
        max_epochs=30,
        accelerator=accelerator,
        devices=devices,
        callbacks=[
            LearningRateMonitor(),
            DeviceStatsMonitor(),
            metric_callback,
        ],
        logger=TensorBoardLogger(save_dir=str(log_dir), name="finetune_eval"),
        precision=precision,
        strategy=strategy,
        num_sanity_val_steps=0,  # NOTE: save shared memory usage
    )
    classifier = FinetuneClassifier(
        model=model,
        batch_size_per_device=batch_size_per_device,
        feature_dim=model.online_classifier.feature_dim,
        num_classes=num_classes,
    )
    trainer.fit(
        model=classifier,
        train_dataloaders=train_dataloader,
        val_dataloaders=val_dataloader,
    )
    metrics_dict: Dict[str, float] = dict()
    for metric in ["val_top1", "val_top5"]:
        print_rank_zero(
            f"max finetune {metric}: {max(metric_callback.val_metrics[metric])}"
        )
        metrics_dict[metric] = max(metric_callback.val_metrics[metric])
    return metrics_dict
