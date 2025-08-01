from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import DeviceStatsMonitor, LearningRateMonitor
from pytorch_lightning.loggers import TensorBoardLogger
from torch.nn import BatchNorm1d, Linear, Module, Sequential
from torch.optim import Optimizer
from torch.utils.data import DataLoader

from lightly.data import LightlyDataset
from lightly.transforms.torchvision_v2_compatibility import torchvision_transforms as T
from lightly.transforms.utils import IMAGENET_NORMALIZE
from lightly.utils.benchmarking import LinearClassifier, MetricCallback
from lightly.utils.dist import print_rank_zero
from lightly.utils.lars import LARS
from lightly.utils.scheduler import CosineWarmupScheduler


class LinearClassifierMAE(LinearClassifier):
    def __init__(
        self,
        model: Module,
        batch_size_per_device: int,
        lr: float = 0.1,
        feature_dim: int = 2048,
        num_classes: int = 1000,
        topk: Tuple[int, ...] = (1, 5),
    ) -> None:
        super().__init__(
            model=model,
            batch_size_per_device=batch_size_per_device,
            lr=lr,
            feature_dim=feature_dim,
            num_classes=num_classes,
            topk=topk,
        )

        # MAE adds an extra batch norm layer before the classification head.
        self.classification_head = Sequential(
            BatchNorm1d(feature_dim, affine=False, eps=1e-6),
            Linear(feature_dim, num_classes),
        )

    # Adapt optimizer to match MAE settings.
    # Type ignore is needed because return type of LightningModule.configure_optimizers
    # is complicated and typing changes between versions.
    def configure_optimizers(  # type: ignore[override]
        self,
    ) -> Tuple[List[Optimizer], List[Dict[str, Union[Any, str]]]]:
        parameters = list(self.get_trainable_parameters())

        optimizer = LARS(
            parameters,
            lr=self.get_effective_lr(),
            momentum=0.9,
            weight_decay=0.0,
        )
        scheduler = {
            "scheduler": CosineWarmupScheduler(
                optimizer=optimizer,
                warmup_epochs=int(
                    self.trainer.estimated_stepping_batches
                    / self.trainer.max_epochs
                    * 10
                ),
                max_epochs=int(self.trainer.estimated_stepping_batches),
            ),
            "interval": "step",
        }
        return [optimizer], [scheduler]


def linear_eval(
    model: Module,
    eval_method: str,
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
    """Runs a linear evaluation on the given model."""
    print_rank_zero("Running linear evaluation...")

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
        max_epochs=90,
        accelerator=accelerator,
        devices=devices,
        callbacks=[
            LearningRateMonitor(),
            DeviceStatsMonitor(),
            metric_callback,
        ],
        logger=TensorBoardLogger(save_dir=str(log_dir), name="linear_eval"),
        precision=precision,
        strategy=strategy,
        num_sanity_val_steps=0,  # NOTE: save shared memory usage
    )
    if eval_method == "mae":
        classifier = LinearClassifierMAE(
            model=model,
            batch_size_per_device=batch_size_per_device,
            feature_dim=model.online_classifier.feature_dim,
            num_classes=num_classes,
        )
        print_rank_zero("Using MAE linear classifier.")
    else:
        classifier = LinearClassifier(
            model=model,
            batch_size_per_device=batch_size_per_device,
            feature_dim=model.online_classifier.feature_dim,
            num_classes=num_classes,
        )
        print_rank_zero("Using SimCLR linear classifier.")

    trainer.fit(
        model=classifier,
        train_dataloaders=train_dataloader,
        val_dataloaders=val_dataloader,
    )
    metrics_dict: Dict[str, float] = dict()
    for metric in ["val_top1", "val_top5"]:
        print_rank_zero(
            f"max linear {metric}: {max(metric_callback.val_metrics[metric])}"
        )
        metrics_dict[metric] = max(metric_callback.val_metrics[metric])

    return metrics_dict
