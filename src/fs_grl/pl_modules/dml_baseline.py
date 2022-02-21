import itertools
import logging
from typing import Any, Mapping, Optional

import hydra
import omegaconf
import pytorch_lightning as pl
import torch
from hydra.utils import instantiate
from torch import nn
from torchmetrics import Accuracy, FBetaScore

from nn_core.common import PROJECT_ROOT
from nn_core.model_logging import NNLogger

from fs_grl.data.datamodule import MetaData
from fs_grl.data.episode import EpisodeBatch
from fs_grl.pl_modules.pl_module import MyLightningModule

pylogger = logging.getLogger(__name__)


class DMLBaseline(MyLightningModule):
    logger: NNLogger

    def __init__(self, metadata: Optional[MetaData] = None, *args, **kwargs) -> None:
        super().__init__()

        self.save_hyperparameters(logger=False, ignore=("metadata",))

        self.metadata = metadata

        # classes should be sorted, might add an assert later
        self.classes = list(metadata.classes_to_label_dict.keys())

        self.model = instantiate(
            self.hparams.model,
            cfg=self.hparams.model,
            feature_dim=self.metadata.feature_dim,
            num_classes=self.metadata.num_classes,
            num_classes_per_episode=self.metadata.num_classes_per_episode,
            _recursive_=False,
        )

        reductions = ["micro", "weighted", "macro", "none"]
        metrics = (("F1", FBetaScore), ("acc", Accuracy))
        self.val_metrics = nn.ModuleDict(
            {
                f"val/{metric_name}/{reduction}": metric(num_classes=self.metadata.num_classes, average=reduction)
                for reduction, (metric_name, metric) in itertools.product(reductions, metrics)
            }
        )
        self.test_metrics = nn.ModuleDict(
            {
                f"test/{metric_name}/{reduction}": metric(num_classes=self.metadata.num_classes, average=reduction)
                for reduction, (metric_name, metric) in itertools.product(reductions, metrics)
            }
        )

        # metrics computed without mapping
        # self.test_metrics = nn.ModuleDict({"test/micro_acc": Accuracy(num_classes=metadata.num_classes_per_episode)})
        # self.val_metrics = nn.ModuleDict({"val/micro_acc": Accuracy(num_classes=metadata.num_classes_per_episode)})

    def forward(self, batch: EpisodeBatch) -> torch.Tensor:
        """
        :return similarities, tensor ~ (B*(N*Q)*N) containing for each episode the similarity
                between each of the N*Q queries and the N label prototypes
        """

        similarities = self.model(batch)

        return similarities

    def step(self, batch, split: str) -> Mapping[str, Any]:

        similarities = self(batch)

        loss = self.model.loss_func(similarities, batch.cosine_targets)
        self.log_dict({f"loss/{split}": loss}, on_epoch=True, on_step=True)

        return {"similarities": similarities, "loss": loss}

    def training_step(self, batch: EpisodeBatch, batch_idx: int) -> Mapping[str, Any]:

        step_out = self.step(batch, "train")
        return step_out

    def validation_step(self, batch: EpisodeBatch, batch_idx: int):
        step_out = self.step(batch, "val")

        # shape (B*(N*Q)*N)
        similarities = step_out["similarities"]

        num_classes_per_episode = batch.episode_hparams.num_classes_per_episode

        # shape (B*(N*Q), N) contains the similarity between the query
        # and the N label prototypes for each of the N*Q queries
        similarities_per_label = similarities.reshape((-1, num_classes_per_episode))

        # shape (B*(N*Q)) contains for each query the most similar label
        pred_labels = torch.argmax(similarities_per_label, dim=-1)

        pred_global_labels = self.map_pred_labels_to_global(
            pred_labels=pred_labels, batch_global_labels=batch.global_labels, num_episodes=batch.num_episodes
        )

        for metric_name, metric in self.val_metrics.items():
            metric(preds=pred_global_labels, target=batch.queries.y)

        self.log_metrics(split="val", on_step=True, on_epoch=True, cm_reset=False)

        return step_out

    def test_step(self, batch: EpisodeBatch, batch_idx: int) -> Mapping[str, Any]:

        step_out = self.step(batch, "test")

        # shape ~(num_episodes * num_queries_per_class * num_classes_per_episode)
        similarities = step_out["similarities"]

        num_classes_per_episode = batch.episode_hparams.num_classes_per_episode
        reshaped_similarities = similarities.reshape((-1, num_classes_per_episode))
        pred_labels = torch.argmax(reshaped_similarities, dim=-1)

        pred_global_labels = self.map_pred_labels_to_global(
            pred_labels=pred_labels, batch_global_labels=batch.global_labels, num_episodes=batch.num_episodes
        )

        for metric_name, metric in self.test_metrics.items():
            metric(preds=pred_global_labels, target=batch.queries.y)

        self.log_metrics(split="test", on_step=True, on_epoch=True, cm_reset=False)

        return step_out

    def map_pred_labels_to_global(self, pred_labels, batch_global_labels, num_episodes):
        """

        :param pred_labels: (B*N*Q)
        :param batch_global_labels: (B*N)
        :param num_episodes: number of episodes in the batch

        :return:
        """
        global_labels_per_episode = batch_global_labels.reshape(num_episodes, -1)
        pred_labels = pred_labels.reshape(num_episodes, -1)

        mapped_labels = []
        for episode_num in range(num_episodes):

            # shape (N)
            episode_global_labels = global_labels_per_episode[episode_num]
            # shape (N*Q)
            episode_pred_labels = pred_labels[episode_num]
            # shape (N*Q)
            episode_mapped_labels = episode_global_labels[episode_pred_labels]

            mapped_labels.append(episode_mapped_labels)

        # shape (B*N*Q)
        mapped_labels = torch.cat(mapped_labels, dim=0)

        return mapped_labels


@hydra.main(config_path=str(PROJECT_ROOT / "conf"), config_name="default")
def main(cfg: omegaconf.DictConfig) -> None:
    """Debug main to quickly develop the Lightning Module.
    Args:
        cfg: the hydra configuration
    """
    _: pl.LightningModule = hydra.utils.instantiate(
        cfg.model,
        optim=cfg.optim,
        _recursive_=False,
    )


if __name__ == "__main__":
    main()
